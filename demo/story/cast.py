"""The people (and bots) of Larkspur Labs. All fictional."""

ORG = "larkspur"
ORG_ID = 91_000_000

# login -> (GitHub user id, display name, role)
PEOPLE: dict[str, tuple[int, str, str]] = {
    "maya-chen": (91_000_001, "Maya Chen", "CTO & co-founder"),
    "ines-duarte": (91_000_002, "Inês Duarte", "CEO & co-founder"),
    "tomas-rivera": (91_000_003, "Tomás Rivera", "Staff backend engineer"),
    "jonah-blake": (91_000_004, "Jonah Blake", "Founding engineer (left Oct 2025)"),
    "sam-whitfield": (91_000_005, "Sam Whitfield", "Frontend lead"),
    "priya-natarajan": (91_000_006, "Priya Natarajan", "Backend engineer, search & data"),
    "lena-park": (91_000_007, "Lena Park", "Frontend & offline engineer"),
    "kofi-mensah": (91_000_008, "Kofi Mensah", "Platform engineer"),
    "ravi-kapoor": (91_000_009, "Ravi Kapoor", "Security & backend engineer"),
}

# Bots whose content Tacit must filter out.
BOTS: dict[str, int] = {
    "dependabot[bot]": 49_699_333,
    "github-actions[bot]": 41_898_282,
    "codecov[bot]": 22_429_695,
}


def user(login: str) -> dict:
    if login in BOTS:
        uid = BOTS[login]
        kind = "Bot"
    else:
        uid = PEOPLE[login][0]
        kind = "User"
    return {
        "login": login,
        "id": uid,
        "node_id": f"{'BOT' if kind == 'Bot' else 'U'}_kgDO{uid}",
        "avatar_url": f"https://avatars.githubusercontent.com/u/{uid}?v=4",
        "html_url": f"https://github.com/{login.removesuffix('[bot]')}"
        if kind == "User"
        else f"https://github.com/apps/{login.removesuffix('[bot]')}",
        "type": kind,
        "site_admin": False,
    }
