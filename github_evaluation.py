from typing import Dict, List, Optional
from dataclasses import dataclass
import os
import re
import shutil
import uuid
import subprocess
import datetime as dt
import pandas as pd
import jira
from requests.exceptions import InvalidURL

REPO_URL_REGEX = re.compile(r"https://github\.com/[A-Za-z0-9\-\_]+/[A-Za-z0-9\-\_]+")
JIRA_URL_REGEX = re.compile(r"https://[A-Za-z0-9\-\_]+\.atlassian\.net")
BENCHMARK_OUTPUT_REGEX = re.compile(r"(Tests:\s\d+/\d+\svalid\nMark:\s\s\d+/\d+\spoints)\n\n$")
BENCHMARK_TEST_REGEX = re.compile(r"(92m|91m)Test\s(\d\d\d)\x1b\[0m:\s([^\n]+?)\s\[\d\d?\spoints?\]")

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


def checkout_last_valid_commit(repo_dir: str, deadline: str) -> None:
    wd = os.getcwd()
    os.chdir(repo_dir)
    commit_info = subprocess.run(
        ["git", "log", f"--before={deadline}", "-1", "--format=%h;%ad"],
        capture_output=True,
        text=True,
        check=True
        ).stdout.strip()
    commit_id, commit_date = commit_info.split(';')
    num_commits_after_deadline = subprocess.run(
        f"git log --after='{deadline}' --oneline | wc -l",
        capture_output=True,
        text=True,
        check=True,
        shell=True
    ).stdout.strip()
    print(f"Checkout commit '{commit_id}' of {commit_date} ({num_commits_after_deadline} commits behind HEAD)")
    subprocess.run(
        ["git", "checkout", commit_id],
        capture_output=True,
        check=True
    )
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
        raise JiraEvalError('JIRA authentication failed: ' + error.text) from error
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


@dataclass
class TestResult:
    test_nr: int
    test_name: str
    passed: bool


@dataclass
class BenchmarkResult:
    overall_results: str
    test_results: list[TestResult]


def run_benchmark(repo_dir: str, game: str, timeout: int = 120) -> BenchmarkResult:
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
    overall_score = BENCHMARK_OUTPUT_REGEX.search(result.stdout)
    if not overall_score:
        raise RunBenchmarkError("No proper Benchmark output (missing 'Tests/Mark' section)")
    test_scores = BENCHMARK_TEST_REGEX.findall(result.stdout)
    test_results = []
    for test in test_scores:
        test_results.append(TestResult(test_nr=int(test[1]), test_name=test[2], passed=test[0] == '92m'))
    benchmark_result = BenchmarkResult(overall_results=overall_score.group(1), test_results=test_results)
    return benchmark_result


def prepare_benchmark_evaluation(
    temp_dir: str,
    benchmark_repo_url: str = "https://github.com/ostaubli/devops_project"
    ) -> str:
    """ Clone the benchmark-repo and install requirements according to the 'requirements.txt'. Return repo path """
    repo_dir = os.path.join(temp_dir, 'master_repo')
    clone_repo(benchmark_repo_url, repo_dir)
    subprocess.run(["pip", "install", "-r", f"{repo_dir}/requirements.txt"], check=True)
    return repo_dir


def run_all_benchmarks(repo_dir: str, benchmark_repo_dir: str) -> dict[str, BenchmarkResult]:
    """
    Replace the benchmark files in 'repo_dir' with the files from 'benchmark_repo_dir' and evaluate
    Returns dict with 'game': benchmark results
    """
    if os.path.exists(os.path.join(repo_dir, 'benchmark')):
        shutil.rmtree(os.path.join(repo_dir, 'benchmark'))
    shutil.copytree(os.path.join(benchmark_repo_dir, 'benchmark'), os.path.join(repo_dir, 'benchmark'))
    shutil.copy(os.path.join(benchmark_repo_dir, 'mypy.ini'), repo_dir)
    shutil.copy(os.path.join(benchmark_repo_dir, '.pylintrc'), repo_dir)
    out: dict[str, BenchmarkResult] = {}
    for game in ['hangman', 'battleship', 'uno', 'dog']:
        try:
            out[game] = run_benchmark(repo_dir, game)
        except RunBenchmarkError as err:
            out[game] = BenchmarkResult(overall_results=str(err), test_results=[])
    return out


@dataclass
class TeamResult:
    git_contributors: str | None
    github_errors: str
    completed_jira_issues: str | None
    benchmark_results: dict[str, BenchmarkResult]


def evaluate_team(team: Team, temp_dir: str, master_repo: str) -> TeamResult:
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
            deadline = os.getenv('DEADLINE', dt.datetime.now().strftime("%Y-%m-%d %H:%M:%s"))
            checkout_last_valid_commit(repo_dir, deadline=deadline)
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
    return TeamResult(
        git_contributors=contributors,
        github_errors=errors,
        completed_jira_issues=jira_eval_results,
        benchmark_results=benchmark_results
    )


def evaluate_teams(sheet_url: str, temp_dir: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    teams = read_team_spreadsheet(sheet_url)
    master_repo = prepare_benchmark_evaluation(temp_dir=temp_dir)
    main_table = []
    uno_table = []
    dog_table = []
    for team in teams[:2]:
        team_result = evaluate_team(team=team, temp_dir=temp_dir, master_repo=master_repo)
        main_table.append({
            "team_id": team.nr,
            "team_name": team.name,
            "repository": team.repository,
            "contributors": team_result.git_contributors,
            "github_errors": team_result.github_errors,
            "jira_board": team.jira_board,
            "completed_jira_issues": team_result.completed_jira_issues,
            "hangman_benchmark": team_result.benchmark_results['hangman'].overall_results
                if 'hangman' in team_result.benchmark_results else '-',
            "battleship_benchmark": team_result.benchmark_results['battleship'].overall_results
                if 'battleship' in team_result.benchmark_results else '-',
            "uno_benchmark": team_result.benchmark_results['uno'].overall_results
                if 'uno' in team_result.benchmark_results else '-',
            "dog_benchmark": team_result.benchmark_results['dog'].overall_results
                if 'dog' in team_result.benchmark_results else '-',
            })
        if 'uno' in team_result.benchmark_results:
            uno_tests = team_result.benchmark_results['uno'].test_results
            if len(uno_tests) > 0:
                uno_test_results: dict[str, str | int | None] = {"team_id": team.nr, "team_name": team.name}
                for test in uno_tests:
                    uno_test_results[f"{test.test_nr}: {test.test_name}"] = 1 if test.passed else 0
                uno_table.append(uno_test_results)
        if 'dog' in team_result.benchmark_results:
            dog_tests = team_result.benchmark_results['dog'].test_results
            if len(dog_tests) > 0:
                dog_test_results: dict[str, str | int | None] = {"team_id": team.nr, "team_name": team.name}
                for test in dog_tests:
                    dog_test_results[f"{test.test_nr}: {test.test_name}"] = 1 if test.passed else 0
                dog_table.append(dog_test_results)
    shutil.rmtree(master_repo)
    return pd.DataFrame(main_table), pd.DataFrame(uno_table), pd.DataFrame(dog_table)


if __name__ == "__main__":

    #sheet_url = "https://docs.google.com/spreadsheets/d/1d2ihVlrR-1paZUCB2vc0b42ah566luzBrqVObEeZsJ8/edit?gid=0#gid=0"
    #teams = read_team_spreadsheet(sheet_url)
    url = os.getenv("SPREADSHEET_URL")
    if url is None:
        raise ValueError('No Spreadsheet URL provided')
    tempdir = os.getenv("TEMPDIR")
    if tempdir is None:
        raise ValueError('No temporary directory provided')
    res, uno, dog = evaluate_teams(url, tempdir)
    res.to_csv('evaluation_results.csv', index=False, sep=';')
    uno.to_csv('uno_test_overview.csv', index=False, sep=';')
    dog.to_csv('dog_test_overview.csv', index=False, sep=';')
