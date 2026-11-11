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


def _extract_workshop_items(html: str) -> dict | None:
    """Вытаскивает список работ из нового JSON-блока страницы мастерской (React SSR)
    и дополняет из HTML-ссылок, если элементов меньше 30."""
    m = re.search(r'<script[^>]*id="valve-ssr-data"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    items_map = {}
    total = 0

    def walk(o):
        nonlocal total
        if isinstance(o, dict):
            if isinstance(o.get("results"), list) and o["results"] and isinstance(o["results"][0], dict) and "publishedfileid" in o["results"][0]:
                for r in o["results"]:
                    pid = str(r.get("publishedfileid", ""))
                    if pid and pid not in items_map:
                        items_map[pid] = {
                            "id": pid,
                            "title": r.get("title", ""),
                            "preview": r.get("preview_url", ""),
                            "file_size": int(r.get("file_size", 0) or 0),
                        }
                if o.get("total_count"):
                    total = int(o.get("total_count"))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and o[:1] in "{[":
            try:
                walk(json.loads(o))
            except Exception:
                pass

    try:
        data = json.loads(m.group(1))
        walk(data)
    except Exception:
        pass

    # Если элементов меньше 30, добираем ID из HTML-ссылок
    if len(items_map) < 30:
        for m_id in re.finditer(r'sharedfiles/filedetails/\?id=(\d+)', html):
            pid = m_id.group(1)
            if pid not in items_map:
                items_map[pid] = {"id": pid, "title": "", "preview": "", "file_size": 0}

    items = list(items_map.values())
    if not items:
        return None

    # Если у каких-то элементов нет заголовков (добраны по ссылкам), подтянем их детали пачкой
    missing_details = [int(x["id"]) for x in items if not x["title"]]
    if missing_details:
        details = fetch_details_bulk(missing_details)
        for it in items:
            if not it["title"] and int(it["id"]) in missing_details:
                d = details.get(it["id"], {})
                it["title"] = d.get("title", "")
                it["preview"] = d.get("preview_url", "")
                it["file_size"] = int(d.get("file_size", 0))

    return {"items": items, "total": total or len(items)}


# Кэш ID для игры, чтобы обходить ограничение Steam SSR на страницах > 3
_WORKSHOP_ID_CACHE = {}

def browse_catalog(appid: int, page: int = 1, numperpage: int = 30, query: str = "") -> dict:
    global _WORKSHOP_ID_CACHE
    cache_key = f"{appid}_{query}"
    
    # Если кэша нет или он старый, соберем все доступные ID со страниц 1-3 (или больше)
    if cache_key not in _WORKSHOP_ID_CACHE or len(_WORKSHOP_ID_CACHE[cache_key]) < 90:
        all_ids = []
        total_count = 0
        # Проходим первые 3 страницы, где Steam гарантированно отдает SSR-данные
        for p in range(1, 4):
            url = f"https://steamcommunity.com/workshop/browse/?appid={appid}&section=readytouseitems&numperpage=30&p={p}"
            if query:
                url += "&searchtext=" + urllib.parse.quote(query)
            try:
                t = _http_get(url)
                parsed = _extract_workshop_items(t)
                if parsed and parsed["items"]:
                    total_count = max(total_count, parsed["total"])
                    for it in parsed["items"]:
                        if it["id"] not in all_ids:
                            all_ids.append(it["id"])
            except Exception:
                pass
        
        # Если вдруг в кэше пусто, попробуем хотя бы первую страницу старым методом
        if not all_ids:
            try:
                t = _http_get(f"https://steamcommunity.com/workshop/browse/?appid={appid}&section=readytouseitems&numperpage=30&p=1")
                for m in re.finditer(r'sharedfiles/filedetails/\?id=(\d+)', t):
                    if m.group(1) not in all_ids:
                        all_ids.append(m.group(1))
                total_count = max(total_count, len(all_ids) * 3) # примерный тотал
            except Exception:
                pass
                
        _WORKSHOP_ID_CACHE[cache_key] = {"ids": all_ids, "total": max(total_count, len(all_ids))}

    cached = _WORKSHOP_ID_CACHE[cache_key]
    all_ids = cached["ids"]
    total = cached["total"]
    
    # Вычисляем срез для запрошенной страницы
    start_idx = (page - 1) * numperpage
    end_idx = start_idx + numperpage
    page_ids = all_ids[start_idx:end_idx]
    
    # Если для этой страницы ID еще не собраны (например, запросили очень далекую страницу),
    # попробуем напрямую запросить HTML этой страницы
    if not page_ids and start_idx < total:
        url = f"https://steamcommunity.com/workshop/browse/?appid={appid}&section=readytouseitems&numperpage=30&p={page}"
        if query:
            url += "&searchtext=" + urllib.parse.quote(query)
        try:
            t = _http_get(url)
            parsed = _extract_workshop_items(t)
            if parsed and parsed["items"]:
                page_ids = [it["id"] for it in parsed["items"][:numperpage]]
        except Exception:
            pass

    # Получаем детальную информацию для текущей страницы пачкой
    numeric_ids = [int(x) for x in page_ids if x.isdigit()]
    details = fetch_details_bulk(numeric_ids) if numeric_ids else {}
    
    items = []
    for pid in page_ids:
        d = details.get(pid, {})
        items.append({
            "id": pid,
            "title": d.get("title", f"Item {pid}"),
            "preview": d.get("preview_url", ""),
            "file_size": int(d.get("file_size", 0)),
        })
        
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
    # Новый формат (React SSR): данные во встроенном JSON
    parsed = _extract_workshop_items(t)
    if parsed:
        return parsed["items"]
    # Старый формат: ссылки filedetails/?id= в HTML
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
