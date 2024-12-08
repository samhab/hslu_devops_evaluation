from typing import Dict, List, Optional
from dataclasses import dataclass
import os
import re
import shutil
import uuid
import subprocess
import pandas as pd
import jira
from requests.exceptions import InvalidURL

REPO_URL_REGEX = re.compile(r"https://github\.com/[A-Za-z0-9\-\_]+/[A-Za-z0-9\-\_]+")
JIRA_URL_REGEX = re.compile(r"https://[A-Za-z0-9\-\_]+\.atlassian\.net")
BENCHMARK_OUTPUT_REGEX = re.compile(r"(Tests:\s\d+/\d+\svalid\nMark:\s\s\d+/\d+\spoints)\n\n$")

@dataclass
class Team:
    id: str
    nr: str
    name: Optional[str]
    repository: Optional[str]
    jira_board: Optional[str]


def strip_repo_url(repo_url: str) -> str:
    match = REPO_URL_REGEX.search(repo_url)
    if match:
        return match.group(0)
    return repo_url


def strip_jira_url(jira_url: str) -> str:
    match = JIRA_URL_REGEX.search(jira_url)
    if match:
        return match.group(0)
    return jira_url


def read_team_spreadsheet(sheet_url: str) -> List[Team]:
    csv_export_url = sheet_url.replace("/edit?gid=", "/export?format=csv&gid=")
    dataframe = pd.read_csv(csv_export_url, header=1, skiprows=0)
    result = []
    for _, team_row in dataframe[~dataframe['Team Nr'].isna()].iterrows():
        repo_url = strip_repo_url(team_row['GitHub Repo URL']) if not pd.isna(team_row['GitHub Repo URL']) else None
        jira_url = strip_jira_url(team_row['Jira Board URL']) if not pd.isna(team_row['Jira Board URL']) else None
        result.append(Team(
            id=str(uuid.uuid4()),
            nr=team_row['Team Nr'] if not pd.isna(team_row['Team Nr']) else None,
            name=team_row['Team Name'] if not pd.isna(team_row['Team Name']) else None,
            repository=repo_url,
            jira_board=jira_url
            ))
    return result


class CloneRepoError(Exception):
    pass


def clone_repo(repo_url: str, clone_dir: str) -> None:
    """ Clone repository to clone_dir. If directory exists, it needs to be empty """
    if not os.path.exists(clone_dir):
        os.makedirs(clone_dir)
    wd = os.getcwd()
    os.chdir(clone_dir)
    try:
        subprocess.run(["git", "clone", repo_url, clone_dir], check=True)
    except subprocess.CalledProcessError as err:
        os.chdir(wd)
        raise CloneRepoError(err) from err
    os.chdir(wd)


def evaluate_commit_hist(repo_dir: str) -> Dict[str, int]:
    """ Get number of commits by user. Return dict with user: num_commits """
    wd = os.getcwd()
    os.chdir(repo_dir)
    result = subprocess.run(
        "git log --pretty=short | git shortlog -n -s",
        capture_output=True,
        text=True,
        check=True,
        shell=True
    )
    os.chdir(wd)
    out = {}
    for statement in result.stdout.strip().split('\n'):
        parts = statement.strip().split("\t")
        if len(parts) == 2:
            out[parts[1].strip()] = int(parts[0].strip())
    return out


def remove_lecturer_contributions(contributors: Dict[str, int]) -> None:
    if 'Oliver Staubli' in contributors:
        del contributors['Oliver Staubli']
    if 'samhab' in contributors:
        del contributors['samhab']


class JiraEvalError(Exception):
    pass


def evaluate_jira_issues(jira_board: str) -> dict[str, int]:
    """ Get the number of JIRA issues with status 'Done' by 'assignee'. Return dict with user: num_issues """
    try:
        j_client = jira.JIRA(
            server=jira_board,
            basic_auth=(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
        )
        j_client.current_user()
    except KeyError as error:
        raise JiraEvalError("Please provide env variables 'JIRA_EMAIL' and 'JIRA_API_TOKEN'") from error
    except InvalidURL as error:
        raise JiraEvalError('Invalid JIRA board url') from error
    except jira.JIRAError as error:
        raise JiraEvalError('JIRA authentication failed') from error
    try:
        jql_query = "statusCategory = Done ORDER BY created DESC"
        issues = j_client.search_issues(jql_query)
    except jira.JIRAError as error:
        raise JiraEvalError("Jira Query issue: " + error.text) from error
    assignees: dict[str, int] = {}
    for issue in issues:
        if isinstance(issue, str): # according to mypy the issues from search are of type 'issue | str'
            continue
        assignee = issue.get_field('assignee')
        if assignee:
            if assignee.displayName in assignees:
                assignees[assignee.displayName] += 1
            else:
                assignees[assignee.displayName] = 1
    return assignees


class RunBenchmarkError(Exception):
    pass


def run_benchmark(repo_dir: str, game: str, timeout: int = 120) -> str:
    wd = os.getcwd()
    os.chdir(repo_dir)
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = repo_dir
        result = subprocess.run(
            ["python", f"benchmark/benchmark_{game}.py", "python", f"{game}.{game.capitalize()}"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=timeout,
            env=env
        )
    except subprocess.TimeoutExpired as err:
        os.chdir(wd)
        raise RunBenchmarkError("Timeout") from err
    os.chdir(wd)
    if result.returncode != 0:
        raise RunBenchmarkError("Benchmark evaluation failed with message: " + result.stderr)
    scores = BENCHMARK_OUTPUT_REGEX.search(result.stdout)
    if scores:
        return scores.group(1)
    raise RunBenchmarkError("No proper Benchmark output (missing 'Tests/Mark' section)")


def prepare_benchmark_evaluation(
    temp_dir: str,
    benchmark_repo_url: str = "https://github.com/ostaubli/devops_project"
    ) -> str:
    """ Clone the benchmark-repo and install requirements according to the 'requirements.txt'. Return repo path """
    repo_dir = os.path.join(temp_dir, 'master_repo')
    clone_repo(benchmark_repo_url, repo_dir)
    subprocess.run(["pip", "install", "-r", f"{repo_dir}/requirements.txt"], check=True)
    return repo_dir


def run_all_benchmarks(repo_dir: str, benchmark_repo_dir: str) -> dict[str, str]:
    """
    Replace the benchmark files in 'repo_dir' with the files from 'benchmark_repo_dir' and evaluate
    Returns dict with 'game': 'eval string'
    """
    if os.path.exists(os.path.join(repo_dir, 'benchmark')):
        shutil.rmtree(os.path.join(repo_dir, 'benchmark'))
    shutil.copytree(os.path.join(benchmark_repo_dir, 'benchmark'), os.path.join(repo_dir, 'benchmark'))
    shutil.copy(os.path.join(benchmark_repo_dir, 'mypy.ini'), repo_dir)
    shutil.copy(os.path.join(benchmark_repo_dir, '.pylintrc'), repo_dir)
    out = {}
    for game in ['hangman', 'battleship', 'uno', 'dog']:
        try:
            out[game] = run_benchmark(repo_dir, game)
        except RunBenchmarkError as err:
            out[game] = str(err)
    return out


def evaluate_teams(sheet_url: str, temp_dir: str) -> pd.DataFrame:
    teams = read_team_spreadsheet(sheet_url)
    master_repo = prepare_benchmark_evaluation(temp_dir=temp_dir)
    out = []
    for team in teams:
        errors = "no errors"
        benchmark_results = {}
        if team.repository is None:
            contributors = None
            errors = "No repository url in spreadsheed"
        else:
            print(f"Check repository of team {team.name} ({team.repository})")
            repo_dir = os.path.join(temp_dir, team.id)
            try:
                clone_repo(team.repository, repo_dir)
            except CloneRepoError as err:
                contributors = None
                errors = f"Error when cloning repo: {err}"
            else:
                eval_results = evaluate_commit_hist(repo_dir)
                remove_lecturer_contributions(eval_results)
                contributors = ", ".join([f"{user} ({commits})" for user, commits in eval_results.items()])
                benchmark_results = run_all_benchmarks(repo_dir, master_repo)
            shutil.rmtree(repo_dir)
        if team.jira_board is None:
            jira_eval_results = None
        else:
            try:
                jira_res = evaluate_jira_issues(team.jira_board)
                jira_eval_results = ", ".join([f"{user} ({issues})" for user, issues in jira_res.items()])
            except JiraEvalError as error:
                jira_eval_results = str(error)
        out.append({
            "team_id": team.nr,
            "team_name": team.name,
            "repository": team.repository,
            "contributors": contributors,
            "github_errors": errors,
            "jira_board": team.jira_board,
            "completed_jira_issues": jira_eval_results,
            "hangman_benchmark": benchmark_results['hangman'] if 'hangman' in benchmark_results else '-',
            "battleship_benchmark": benchmark_results['battleship'] if 'battleship' in benchmark_results else '-',
            "uno_benchmark": benchmark_results['uno'] if 'uno' in benchmark_results else '-',
            "dog_benchmark": benchmark_results['dog'] if 'dog' in benchmark_results else '-'
            })
    shutil.rmtree(master_repo)
    return pd.DataFrame(out)


if __name__ == "__main__":

    #sheet_url = "https://docs.google.com/spreadsheets/d/1d2ihVlrR-1paZUCB2vc0b42ah566luzBrqVObEeZsJ8/edit?gid=0#gid=0"
    #teams = read_team_spreadsheet(sheet_url)
    url = os.getenv("SPREADSHEET_URL")
    if url is None:
        raise ValueError('No Spreadsheet URL provided')
    tempdir = os.getenv("TEMPDIR")
    if tempdir is None:
        raise ValueError('No temporary directory provided')
    res = evaluate_teams(url, tempdir)
    res.to_csv('evaluation_results.csv', index=False, sep=';')
