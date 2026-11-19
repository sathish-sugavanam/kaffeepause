import base64
import json
import urllib.request


def remote_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def git_auth_env(token: str) -> dict[str, str]:
    """Environment that makes git send the token to github.com.

    The token travels in an environment-only config entry, so it is never in a
    remote URL, never written to .git/config, and never in a command line that
    could end up in an error message.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def create_pull_request(
    token: str,
    repo: str,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
    draft: bool = False,
) -> str:
    """Opens a pull request (a draft when asked) and returns its URL."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls",
        data=json.dumps(
            {"title": title, "head": head, "base": base, "body": body, "draft": draft}
        ).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kaffeepause",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["html_url"]
