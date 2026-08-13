"""GitHub Projects v2, over GraphQL.

Projects v2 has no REST API at all, and PyGithub therefore cannot reach it.
This is the smallest thing that can read a board's single-select fields and set
one on an issue — enough to move a card from Discord.

The token needs the `project` scope on top of `repo`. Without it every query
here comes back `INSUFFICIENT_SCOPES`, which is at least a clear message.
"""

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

GRAPHQL = "https://api.github.com/graphql"
TIMEOUT = aiohttp.ClientTimeout(total=15)


class ProjectError(Exception):
    """A query GitHub refused, with the message it gave."""


async def _query(token: str, query: str, variables: dict) -> dict:
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post(
            GRAPHQL,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            body = await response.json()

    # GraphQL answers 200 with an `errors` array rather than a status code, so
    # the only way to notice a refusal is to look for it.
    if body.get("errors"):
        raise ProjectError(body["errors"][0].get("message", "unknown GraphQL error"))
    return body["data"]


_ITEM = """
query($owner:String!, $repo:String!, $number:Int!, $field:String!) {
  repository(owner:$owner, name:$repo) {
    issue(number:$number) {
      id
      projectItems(first: 10) {
        nodes {
          id
          project {
            id
            title
            field(name: $field) {
              ... on ProjectV2SingleSelectField { id name options { id name } }
            }
          }
          fieldValueByName(name: $field) {
            ... on ProjectV2ItemFieldSingleSelectValue { name optionId }
          }
        }
      }
    }
  }
}
"""

_SET = """
mutation($project:ID!, $item:ID!, $field:ID!, $option:String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: { singleSelectOptionId: $option }
  }) { projectV2Item { id } }
}
"""


async def find_item(token: str, repo_full_name: str, number: int, field: str = "Status") -> dict | None:
    """The board entry for an issue, along with the field's options.

    `None` when the issue is not on any board — which is not an error, just an
    issue nobody has filed yet.
    """
    owner, name = repo_full_name.split("/", 1)
    data = await _query(
        token, _ITEM, {"owner": owner, "repo": name, "number": number, "field": field}
    )
    issue = (data.get("repository") or {}).get("issue") or {}
    for item in issue.get("projectItems", {}).get("nodes", []):
        project = item.get("project") or {}
        if project.get("field"):
            return {
                "item_id": item["id"],
                "project_id": project["id"],
                "project_title": project.get("title", ""),
                "field_id": project["field"]["id"],
                "options": {o["name"]: o["id"] for o in project["field"]["options"]},
                "current": (item.get("fieldValueByName") or {}).get("name"),
            }
    return None


async def set_field(token: str, item: dict, option_name: str) -> str:
    """Moves a card to one of the field's options.

    Matching is case-insensitive because "in progress" is what a person types
    and "In progress" is what the board calls it.
    """
    wanted = option_name.strip().lower()
    for name, option_id in item["options"].items():
        if name.lower() == wanted:
            await _query(
                token,
                _SET,
                {
                    "project": item["project_id"],
                    "item": item["item_id"],
                    "field": item["field_id"],
                    "option": option_id,
                },
            )
            return name
    raise ProjectError(
        f"沒有這個選項。可用的是：{', '.join(item['options'])}"
    )


def token() -> str | None:
    """The token to talk to the board with.

    The bot's own, not a person's: moving a card is bookkeeping, and requiring
    every collaborator to re-authorise with the `project` scope to do it would
    put the feature out of reach of exactly the people who use the board.
    """
    return os.getenv("GITHUB_BOT_TOKEN")
