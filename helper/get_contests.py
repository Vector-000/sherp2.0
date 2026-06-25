import asyncio
import json

import bs4
from aiohttp import ClientSession
from bs4 import BeautifulSoup


async def get_contests():
    http = ClientSession()
    contests = []

    async with http.get("https://open.kattis.com/problem-sources") as resp:
        assert resp.status == 200, "Kattis responsed with non 200 status code!"

        html = await resp.text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")

        if soup.tbody is None:
            raise RuntimeError("Could not find Kattis contests table")

        for item in soup.tbody.children:
            if type(item) is not bs4.element.Tag:
                continue
            if item.a is None:
                continue

            href = item.a.get("href")
            if not isinstance(href, str):
                continue

            contest = href.split("/")[-1]
            assert contest, "Contest url not found"

            contests.append(contest)

    data = {"contests": contests}
    with open("../data/contests.json", "w") as f:
        json.dump(data, f)

    print("Done importing contests!")
    await http.close()


asyncio.run(get_contests())
