import time
from requests.exceptions import ConnectionError
from typing import Optional

import click
import pickledb
from github.GithubException import GithubException, UnknownObjectException
from dotenv import load_dotenv
from openai import BadRequestError, InternalServerError, OpenAI
from tqdm import tqdm

from utils import *


# Load dot file
load_dotenv()

# Create Gorse client
gorse_client = Gorse("http://127.0.0.1:8088", os.getenv("GORSE_API_KEY"))

# Create GitHub client.
github_client = Github(os.getenv("GITHUB_ACCESS_TOKEN"))

# Create GraphQL client.
openai_client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_API_BASE")
)


@click.group()
def command():
    pass


@command.command()
@click.argument("item_id")
def upsert_repo(item_id):
    """Upsert a repository into GitRec."""
    repo = get_repo_info(github_client, item_id)
    gorse_client.insert_item(repo)
    print(repo)


def search_and_upsert(
    db: pickledb.PickleDB, topic: Optional[str] = None, language: Optional[str] = None
):
    query = "stars:>100"
    if topic is not None:
        query += " topic:" + topic
    if language is not None:
        query += " language:" + language
    print("Upsert " + query)
    repos = github_client.search_repositories(query)
    for repo in tqdm(repos):
        # Skip existed repo.
        if not db.exists("repo"):
            db.dcreate("repo")
        if db.dexists("repo", repo.full_name):
            continue
        # Fetch labels.
        labels = [topic for topic in repo.get_topics()]
        if repo.language is not None and repo.language not in labels:
            labels.append(repo.language.lower())
        # Optimize labels
        item = {
            "ItemId": repo.full_name.replace("/", ":").lower(),
            "Timestamp": str(repo.updated_at),
            "Labels": labels,
            "Categories": generate_categories(labels),
            "Comment": repo.description,
        }
        # Truncate long comment
        if item["Comment"] is not None and len(item["Comment"]) > MAX_COMMENT_LENGTH:
            item["Comment"] = item["Comment"][:MAX_COMMENT_LENGTH]
        gorse_client.insert_item(item)
        db.dadd("repo", (repo.full_name, None))


@command.command()
def upsert_repos():
    """Upsert popular repositories (stars >= 100) into GitRec"""
    # Load checkpoint
    db = pickledb.load("checkpoint.db", True)
    # Load existed topics
    topics = set()
    cursor = ""
    while True:
        items, cursor = gorse_client.get_items(1000, cursor)
        for item in items:
            if item["Labels"] is not None:
                for topic in item["Labels"]:
                    topics.add(topic)
        if cursor == "":
            break
    # Search and upsert
    if not db.exists("topic"):
        db.dcreate("topic")
    for topic in topics:
        if not db.dexists("topic", topic):
            while True:
                try:
                    search_and_upsert(db, topic=topic)
                    db.dadd("topic", (topic, None))
                    break
                except RateLimitExceededException as e:
                    print(e)
                    time.sleep(1800)
                    continue
                except Exception as e:
                    print(e)
                    time.sleep(60)


@command.command()
def upgrade_items():
    """Upgrade items in Gorse."""
    cursor = ""
    while True:
        items, cursor = gorse_client.get_items(1000, cursor)
        if cursor == "":
            break
        for item in items:
            if type(item["Labels"]) == list:
                # Fetch repo
                try:
                    repo = github_client.get_repo(item["ItemId"].replace(":", "/"))
                except UnknownObjectException as e:
                    gorse_client.delete_item(item["ItemId"])
                    print("DELETE " + item["ItemId"] + " " + str(e))
                    continue
                except GithubException as e:
                    if e.status == 451:
                        gorse_client.delete_item(item["ItemId"])
                        print("DELETE " + item["ItemId"] + " " + str(e))
                    elif e.status and e.data["message"] in ("Repository access blocked"):
                        gorse_client.delete_item(item["ItemId"])
                        print("DELETE " + item["ItemId"] + " " + str(e))
                    else:
                        print("FAIL " + item["ItemId"] + " " + str(e))
                    continue

                # Delete repo with less than 100 stars
                if repo.stargazers_count < 100:
                    gorse_client.delete_item(item["ItemId"])
                    print("DELETE " + repo.full_name)
                    continue

                # Delete repp with description longer than 1000 characters
                if repo.description is not None and len(repo.description) > MAX_COMMENT_LENGTH:
                    gorse_client.delete_item(item["ItemId"])
                    print("DELETE " + repo.full_name + " with long description")
                    continue

                # Generate categories and labels
                try:
                    language = None
                    languages = repo.get_languages()
                    if len(languages) > 0:
                        language = [max(languages, key=languages.get).lower()]
                    description = repo.description
                    if description is None:
                        description = tldr(repo.get_readme().decoded_content.decode('utf-8'))
                        print('QWEN:', description)
                    description_embedding = embedding(description)
                except BadRequestError as e:
                    print("FAIL " + repo.full_name + " " + str(e))
                    continue
                except InternalServerError as e:
                    print("FAIL " + repo.full_name + " " + str(e))
                    continue
                except ConnectionError as e:
                    print("FAIL " + repo.full_name + " " + str(e))
                    continue
                except UnknownObjectException as e:
                    gorse_client.delete_item(item["ItemId"])
                    print("DELETE " + item["ItemId"] + " " + str(e))
                    continue
                except AssertionError as e:
                    print("FAIL " + repo.full_name + " " + str(e))
                    continue

                # Update item
                gorse_client.update_item(
                    item["ItemId"],
                    categories=language,
                    labels={
                        "embedding": description_embedding,
                        "topics": repo.get_topics(),
                    },
                    timestamp=repo.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    comment=repo.description,
                )
                print("UPDATE " + repo.full_name)


@command.command()
def upgrade_embedding():
    """Upgrade embeddings."""
    cursor = ""
    while True:
        items, cursor = gorse_client.get_items(1000, cursor)
        if cursor == "":
            break
        for item in tqdm(items):
            if len(item['Comment']) > 0:
                item['Labels']['embedding'] = embedding(item['Comment'])
                gorse_client.update_item(
                    item["ItemId"],
                    labels=item['Labels'],
                )


if __name__ == "__main__":
    command()
