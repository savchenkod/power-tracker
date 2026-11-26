"""Проверяет, онлайн ли реле Buckler (= есть ли входное напряжение),
шлёт сообщение в Telegram при смене состояния и отвечает на команду /status.

Переменные окружения (задаются в GitHub Secrets):
  RELAY_SN            серийный номер реле, например 2644_xxxxxx
  TELEGRAM_BOT_TOKEN  токен бота от @BotFather
  TELEGRAM_CHAT_ID    id чата, куда слать сообщения
  RELAY_NAME          (необязательно) как называть объект в сообщениях
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

STALE_SECONDS = 180  # нет связи дольше 3 минут -> считаем, что света нет
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
try:
    TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # на Windows без пакета tzdata
    TZ = timezone.utc


def http_json(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "power-tracker"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def tg(method, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(params).encode()
    return http_json(f"https://api.telegram.org/bot{token}/{method}", data=data)


def send_telegram(text):
    tg("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text)


def get_relay(sn):
    return http_json(f"https://buckler.pw/api/relay/{sn}")["relay"]


def fmt_time(iso):
    if not iso:
        return "?"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
    return dt.strftime("%d.%m %H:%M")


def fmt_duration(seconds):
    m = int(seconds // 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return f"{d} д {h} ч {m} мин"
    return f"{h} ч {m} мин" if h else f"{m} мин"


def status_text(name, online, since, last_conn, now):
    head = f"💡 {name}: свет есть" if online else f"🔌 {name}: света нет (реле офлайн)"
    lines = [head]
    if since:
        lines.append("В этом состоянии: " + fmt_duration((now - datetime.fromisoformat(since)).total_seconds()))
    lines.append(f"Последняя связь реле: {fmt_time(last_conn)}")
    lines.append(f"Проверено: {now.astimezone(TZ).strftime('%d.%m %H:%M')}")
    return "\n".join(lines)


def pending_status_requests(state):
    """Забирает новые сообщения боту; True, если из нашего чата пришёл /status."""
    resp = tg("getUpdates", offset=state.get("tg_offset", 0), timeout=0)
    asked = False
    my_chat = str(os.environ["TELEGRAM_CHAT_ID"])
    for upd in resp.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip().lower()
        if str(msg.get("chat", {}).get("id")) == my_chat and text.startswith(("/status", "статус")):
            asked = True
    return asked


def main():
    sn = os.environ["RELAY_SN"]
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    before = json.dumps(state, sort_keys=True)
    now = datetime.now(timezone.utc)

    try:
        asked = pending_status_requests(state)
    except Exception as e:
        print(f"Не удалось прочитать сообщения Telegram: {e}")
        asked = False

    try:
        relay = get_relay(sn)
    except Exception as e:  # сайт недоступен — не считаем это отключением света
        print(f"Не удалось получить данные: {e}")
        if asked:
            send_telegram(f"⚠️ Сайт Buckler сейчас не отвечает, статус неизвестен.\n{e}")
        relay = None

    if relay is not None:
        last_conn = relay.get("lastConnection")
        # Флаг lastOnlineStatus сервер сбрасывает с большой задержкой, поэтому,
        # как и сам сайт Buckler, смотрим на давность последней связи реле.
        age = None
        if last_conn:
            age = (now - datetime.fromisoformat(last_conn.replace("Z", "+00:00"))).total_seconds()
        online = bool(relay.get("lastOnlineStatus")) and age is not None and age < STALE_SECONDS
        name = os.environ.get("RELAY_NAME") or "Дом"
        print(f"online={online} lastConnection={last_conn} age={age} prev={state.get('online')} asked={asked}")

        if state.get("online") != online:
            if "online" not in state:
                text = f"🔎 Трекер запущен. {name}: " + ("свет есть ✅" if online else "реле офлайн ❌")
            else:
                since = state.get("since")
                dur = ""
                if since:
                    secs = (now - datetime.fromisoformat(since)).total_seconds()
                    dur = (" (света не было " if online else " (свет был ") + fmt_duration(secs) + ")"
                if online:
                    text = f"💡 Свет появился — {name}{dur}"
                else:
                    text = (f"🔌 Света нет — {name}: реле офлайн{dur}\n"
                            f"Последняя связь: {fmt_time(last_conn)}")
            send_telegram(text)
            state["online"] = online
            # для отключения точнее брать момент последней связи реле
            state["since"] = (last_conn.replace("Z", "+00:00") if (not online and last_conn)
                              else now.isoformat())
        elif asked:
            send_telegram(status_text(name, online, state.get("since"), last_conn, now))

    if json.dumps(state, sort_keys=True) != before:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
