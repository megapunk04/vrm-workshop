import asyncio
import json
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from astra_plugin_sdk import Plugin, tool, ui_call, ui_page

BASE = Path(__file__).resolve().parent
DEPOTDL = BASE / "bin" / "depotdl" / "DepotDownloaderMod.exe"
CACHE = BASE / "ui" / "cache"
DOWNLOADS = Path.home() / "Downloads"
HISTORY = BASE / "history.json"
MAX_HISTORY = 30
STEAM_API = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def parse_workshop_id(url_or_id: str) -> int:
    s = url_or_id.strip()
    if s.isdigit():
        return int(s)
    q = urllib.parse.urlparse(s)
    params = urllib.parse.parse_qs(q.query)
    if "id" in params and params["id"][0].isdigit():
        return int(params["id"][0])
    raise ValueError("Это не ссылка на мастерскую Steam и не ID работы")


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "ignore")


def fetch_details(item_id: int) -> dict:
    body = f"itemcount=1&publishedfileids[0]={item_id}"
    req = urllib.request.Request(STEAM_API, data=body.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    d = data["response"]["publishedfiledetails"][0]
    return {
        "appid": int(d.get("consumer_app_id") or d.get("creator_app_id") or 0),
        "title": d.get("title", f"item_{item_id}"),
        "preview_url": d.get("preview_url", ""),
        "file_size": int(d.get("file_size", 0)),
    }


def fetch_details_bulk(item_ids: list[int]) -> dict:
    out = {}
    for i in range(0, len(item_ids), 100):
        chunk = item_ids[i:i + 100]
        parts = [f"itemcount={len(chunk)}"]
        parts += [f"publishedfileids[{j}]={pid}" for j, pid in enumerate(chunk)]
        body = "&".join(parts)
        req = urllib.request.Request(STEAM_API, data=body.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        for d in data["response"]["publishedfiledetails"]:
            out[str(d.get("publishedfileid"))] = {
                "title": d.get("title", ""),
                "preview_url": d.get("preview_url", ""),
                "file_size": int(d.get("file_size", 0)),
            }
    return out


def search_app(query: str) -> list[dict]:
    url = "https://store.steampowered.com/api/storesearch/?term=" + urllib.parse.quote(query) + "&l=russian&cc=RU"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode())
    out = []
    for it in data.get("items", []):
        if it.get("type") != "app":
            continue
        out.append({"appid": it["id"], "name": it.get("name", ""), "image": it.get("tiny_image", "")})
    return out


def browse_catalog(appid: int, page: int = 1, numperpage: int = 30, query: str = "") -> dict:
    url = f"https://steamcommunity.com/workshop/browse/?appid={appid}&section=readytouseitems&numperpage={numperpage}&p={page}"
    if query:
        url += "&searchtext=" + urllib.parse.quote(query)
    t = _http_get(url)
    ids = []
    for m in re.finditer(r'sharedfiles/filedetails/\?id=(\d+)', t):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    total = 0
    mt = re.search(r'class="workshopBrowsePagingInfo[^"]*"[^>]*>\s*([\d,]+)', t)
    if mt:
        total = int(mt.group(1).replace(",", ""))
    details = fetch_details_bulk([int(x) for x in ids])
    items = []
    for pid in ids:
        d = details.get(pid, {})
        items.append({"id": pid, "title": d.get("title", ""), "preview": d.get("preview_url", ""), "file_size": d.get("file_size", 0)})
    return {"items": items, "total": total, "page": page}


def run_depotdl(appid: int, item_id: int, out_dir: Path) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(DEPOTDL), "-app", str(appid), "-pubfile", str(item_id), "-dir", str(out_dir)],
        capture_output=True, text=True, timeout=540,
    )
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def find_model(out_dir: Path) -> Path | None:
    for ext in ("*.vrm", "*.glb", "*.gltf"):
        hits = list(out_dir.rglob(ext))
        if hits:
            return hits[0]
    return None


def _safe_name(title: str, item_id: int) -> str:
    s = "".join(c for c in title if c not in '\\/:*?"<>|').strip()
    return s or f"item_{item_id}"


def _skin_key(title: str) -> str:
    """Ключ персонажа из названия работы: убираем префиксы/суффиксы-скины."""
    t = re.sub(r'^[^A-Za-zА-Яа-яЁё0-9]+', '', title or '')
    t = re.sub(r'\b(skin|ver\.?|v\d+(\.\d+)*|breakdown|white|black|lavender|shiro|red nightshade|次生银翼|幻滅之形|照世相|墨形影蹤)\b.*$', '', t, flags=re.IGNORECASE)
    # для китайских имён в скобках берём латиницу до неё
    m = re.match(r'^([A-Za-z][A-Za-z0-9 .\'\-]*)', t)
    return (m.group(1).strip().lower() if m else t.strip().lower())


def search_skins(appid: int, title: str) -> list[dict]:
    """Ищет другие работы того же персонажа (скины) в мастерской."""
    key = _skin_key(title)
    if len(key) < 2:
        return []
    url = f"https://steamcommunity.com/workshop/browse/?appid={appid}&section=readytouseitems&numperpage=30&p=1&searchtext=" + urllib.parse.quote(key)
    t = _http_get(url)
    ids = []
    for m in re.finditer(r'sharedfiles/filedetails/\?id=(\d+)', t):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    if not ids:
        return []
    details = fetch_details_bulk([int(x) for x in ids])
    out = []
    for pid in ids:
        d = details.get(pid, {})
        out.append({"id": pid, "title": d.get("title", ""), "preview": d.get("preview_url", ""), "file_size": d.get("file_size", 0)})
    return out


def _load_history() -> list:
    if not HISTORY.exists():
        return []
    try:
        return json.loads(HISTORY.read_text("utf-8"))
    except Exception:
        return []


def _save_history(items: list) -> None:
    HISTORY.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")


def _push_history(entry: dict) -> None:
    items = _load_history()
    items = [x for x in items if x.get("appid") != entry.get("appid")]
    items.insert(0, entry)
    _save_history(items[:MAX_HISTORY])


@ui_page("vrm-workshop", "VRM Workshop", "index.html")
class VrmWorkshop(Plugin):
    def __init__(self):
        super().__init__()
        CACHE.mkdir(parents=True, exist_ok=True)

    async def _ensure_model(self, url_or_id: str) -> dict:
        item_id = parse_workshop_id(url_or_id)
        details = await asyncio.to_thread(fetch_details, item_id)
        if not details["appid"]:
            raise RuntimeError("Не удалось определить игру (appid) для этой работы")
        target = CACHE / f"{item_id}.vrm"
        if not target.exists():
            out = BASE / "cache" / f"dl_{item_id}"
            log = await asyncio.to_thread(run_depotdl, details["appid"], item_id, out)
            model = find_model(out)
            if model is None:
                raise RuntimeError("DepotDownloaderMod не нашёл модель. " + log[-1500:])
            model.replace(target)
        return {
            "vrm_url": f"cache/{item_id}.vrm",
            "title": details["title"],
            "preview_url": details["preview_url"],
            "file_size": details["file_size"],
        }

    @ui_call("search-app")
    async def search_app(self, query: str = ""):
        items = await asyncio.to_thread(search_app, query)
        for it in items:
            _push_history({
                "appid": it["appid"],
                "name": it["name"],
                "image": it["image"],
                "query": query,
                "ts": int(time.time()),
            })
        return {"items": items}

    @ui_call("history")
    async def history(self):
        return _load_history()

    @ui_call("clear-history")
    async def clear_history(self):
        if HISTORY.exists():
            HISTORY.unlink()
        return []

    @ui_call("catalog")
    async def catalog(self, appid: int = 0, page: int = 1, query: str = ""):
        return await asyncio.to_thread(browse_catalog, int(appid), int(page), 30, query)

    @ui_call("skins")
    async def skins(self, appid: int = 0, title: str = ""):
        return await asyncio.to_thread(search_skins, int(appid), title)

    @ui_call("preview")
    async def preview(self, url: str = ""):
        return await self._ensure_model(url)

    @ui_call("download")
    async def download(self, url: str = ""):
        info = await self._ensure_model(url)
        item_id = parse_workshop_id(url)
        src = CACHE / f"{item_id}.vrm"
        dest = DOWNLOADS / f"{_safe_name(info['title'], item_id)}.vrm"
        if dest.exists():
            dest.unlink()
        shutil.copy2(src, dest)
        return {"path": str(dest)}

    @tool("Скачать VRM-модель из мастерской Steam по ссылке или ID. Возвращает путь к файлу.")
    async def download_vrm_workshop(self, url: str):
        info = await self._ensure_model(url)
        item_id = parse_workshop_id(url)
        src = CACHE / f"{item_id}.vrm"
        dest = DOWNLOADS / f"{_safe_name(info['title'], item_id)}.vrm"
        if dest.exists():
            dest.unlink()
        shutil.copy2(src, dest)
        return {"title": info["title"], "path": str(dest), "size": info["file_size"]}


if __name__ == "__main__":
    VrmWorkshop().run()
