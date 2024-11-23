from typing import Dict, List, Optional
from dataclasses import dataclass
import os
import shutil
import uuid
import subprocess
import pandas as pd


@dataclass
class Team:
    id: str
    nr: str
    name: Optional[str]
    repository: Optional[str]


def read_team_spreadsheet(sheet_url: str) -> List[Team]:
    csv_export_url = sheet_url.replace("/edit?gid=", "/export?format=csv&gid=")
    dataframe = pd.read_csv(csv_export_url, header=1, skiprows=0)
    result = []
    for _, team_row in dataframe[~dataframe['Team Nr'].isna()].iterrows():
        result.append(Team(
            id=str(uuid.uuid4()),
            nr=team_row['Team Nr'] if not pd.isna(team_row['Team Nr']) else None,
            name=team_row['Team Name'] if not pd.isna(team_row['Team Name']) else None,
            repository=team_row['GitHub Repo URL'] if not pd.isna(team_row['GitHub Repo URL']) else None
            ))
    return result


def clone_repo(repo_url: str, clone_dir: str) -> None:
    """ Clone repository to clone_dir. If directory exists, it needs to be empty """
    if not os.path.exists(clone_dir):
        os.makedirs(clone_dir)
    wd = os.getcwd()
    os.chdir(clone_dir)
    subprocess.run(["git", "clone", repo_url, clone_dir], check=True)
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


def check_repos(sheet_url: str, temp_dir: str) -> pd.DataFrame:
    teams = read_team_spreadsheet(sheet_url)
    out = []
    errors = "no errors"
    for team in teams:
        if team.repository is None:
            passed = False
            contributors = None
            errors = "No repository url in spreadsheed"
        else:
            print(f"Check repository of team {team.name} ({team.repository})")
            repo_dir = os.path.join(temp_dir, team.id)
            try:
                clone_repo(team.repository, repo_dir)
            except subprocess.CalledProcessError as err:
                passed = False
                contributors = None
                errors = f"Error when cloning repo: {err}"
            else:
                eval_results = evaluate_commit_hist(repo_dir)
                print(eval_results)
                passed = len(eval_results) == 5 and min(eval_results.values()) > 4
                contributors = ", ".join([f"{user} ({commits})" for user, commits in eval_results.items()])
            shutil.rmtree(repo_dir)
        out.append({
            "team_id": team.nr,
            "team_name": team.name,
            "repository": team.repository,
            "passed": passed,
            "contributors": contributors,
            "errors": errors
            })
    return pd.DataFrame(out)


if __name__ == "__main__":

    #sheeturl = "https://docs.google.com/spreadsheets/d/1d2ihVlrR-1paZUCB2vc0b42ah566luzBrqVObEeZsJ8/edit?gid=0#gid=0"
    #df = read_team_spreadsheet(sheeturl)
    url = os.getenv("SPREADSHEET_URL")
    if url is None:
        raise ValueError('No Spreadsheet URL provided')
    tempdir = os.getenv("TEMPDIR")
    if tempdir is None:
        raise ValueError('No temporary directory provided')
    res = check_repos(url, tempdir)
    res.to_csv('evaluation_results.csv', index=False, sep=';')
