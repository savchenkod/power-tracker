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

STALE_SECONDS = 120  # нет связи дольше 2 минут -> считаем, что света нет
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


def voltage_line(sn, relay, now, after=None):
    """Строка с последним напряжением по фазам (минутные данные Buckler).

    after — не брать показания старше этого момента (ISO), чтобы после включения
    света не показать значения, снятые ещё до отключения. Возвращает None, если
    свежих данных пока нет (они появляются с задержкой 1–2 минуты).
    """
    to_ms = int(now.timestamp() * 1000)
    from_ms = to_ms - 300_000
    if after:
        from_ms = max(from_ms, int(datetime.fromisoformat(after).timestamp() * 1000) + 1)
    try:
        data = http_json(f"https://buckler.pw/api/relay/{sn}/readings"
                         f"?from={from_ms}&to={to_ms}&type=voltage").get("data") or []
        data = [d for d in data if d.get("timestamp", 0) >= from_ms]
        actual = data[-1]["voltage"]["actual"]
    except Exception as e:
        print(f"Нет данных о напряжении: {e}")
        return None
    vs = (relay.get("settings") or {}).get("voltage") or {}
    vmin, vmax = vs.get("min", 198), vs.get("max", 242)
    parts = []
    for i, v in enumerate(actual, 1):
        mark = " ⚠️" if v < vmin or v > vmax else ""
        parts.append(f"L{i}: {round(v)} В{mark}")
    return "Напряжение: " + " · ".join(parts)


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


def status_text(name, online, since, last_conn, now, volts=None):
    head = f"💡 {name}: свет есть" if online else f"🔌 {name}: света нет (реле офлайн)"
    lines = [head]
    if volts:
        lines.append(volts)
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
            state.pop("volts_after", None)
            if "online" not in state:
                text = f"🔎 Трекер запущен. {name}: " + ("свет есть ✅" if online else "реле офлайн ❌")
                if online:
                    text += "\n" + (voltage_line(sn, relay, now) or "Напряжение: данных пока нет")
            else:
                since = state.get("since")
                dur = ""
                if since:
                    secs = (now - datetime.fromisoformat(since)).total_seconds()
                    dur = (" (света не было " if online else " (свет был ") + fmt_duration(secs) + ")"
                if online:
                    text = f"💡 Свет появился — {name}{dur}"
                    # since здесь — момент последней связи перед отключением
                    volts = voltage_line(sn, relay, now, after=since)
                    if volts:
                        text += "\n" + volts
                    else:
                        text += "\nНапряжение по фазам пришлю следующим сообщением"
                        state["volts_after"] = since or now.isoformat()
                else:
                    text = (f"🔌 Света нет — {name}: реле офлайн{dur}\n"
                            f"Последняя связь: {fmt_time(last_conn)}")
            send_telegram(text)
            state["online"] = online
            # для отключения точнее брать момент последней связи реле
            state["since"] = (last_conn.replace("Z", "+00:00") if (not online and last_conn)
                              else now.isoformat())
        else:
            if online and state.get("volts_after"):
                volts = voltage_line(sn, relay, now, after=state["volts_after"])
                if volts:
                    send_telegram(f"⚡ {name}, после включения света:\n{volts}")
                    state.pop("volts_after")
            if asked:
                volts = (voltage_line(sn, relay, now) or "Напряжение: данных пока нет") if online else None
                send_telegram(status_text(name, online, state.get("since"), last_conn, now, volts))

    if json.dumps(state, sort_keys=True) != before:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
