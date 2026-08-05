"""
find spiritfont soom removals that happen at the same exact timestamp
as another buff's removal on the same target - testing to see whether spiritfont
is actually removing buffs or just coincidental
"""
import sys
import os
import itertools
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stdout
import requests

# get wcl api keys via https://www.warcraftlogs.com/api/docs
# set environment variable or just replace inline with quotes
# `CLIENT_ID = "your_key_here"` etc
CLIENT_ID = os.environ["WCL_CLIENT_ID"]
CLIENT_SECRET = os.environ["WCL_CLIENT_SECRET"]
SPIRITFONT_ID = 1260617
MASS_REMOVAL_THRESHOLD = 4
NEARBY_WINDOW_MS = 500
EXCLUDED_ABILITY_IDS = {
    119611, # rem
    1244893, # beacon of the savior buff
    1245369, # beacon of the savior absorb
}


class Spinner:
    def __init__(self, message):
        self.message = message
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        for frame in itertools.cycle("|/-\\"):
            if self._stop.is_set():
                break
            print(f"\r{self.message} {frame}", end="", flush=True, file=sys.stderr)
            time.sleep(0.1)
        print(f"\r{' ' * (len(self.message) + 2)}\r", end="", flush=True, file=sys.stderr)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        self._thread.join()


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def get_token():
    resp = requests.post(
        "https://www.warcraftlogs.com/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(CLIENT_ID, CLIENT_SECRET),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def gql(token, query, variables):
    resp = requests.post(
        "https://www.warcraftlogs.com/api/v2/client",
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(payload["errors"])
    return payload["data"]


def get_fight_data(token, report_code):
    """returns:
        {fight_id: (startTime, endTime)} 
        and {fight_id: pull_number}
    """
    query = """
    query($code: String!) {
      reportData {
        report(code: $code) {
          fights { id difficulty encounterID startTime endTime }
        }
      }
    }
    """
    data = gql(token, query, {"code": report_code})
    fights = data["reportData"]["report"]["fights"]

    real_fights = [f for f in fights if f["encounterID"] != 0]
    pull_numbers = {f["id"]: i for i, f in enumerate(real_fights, start=1)}

    fight_windows = {
        f["id"]: (f["startTime"], f["endTime"]) for f in fights if f["difficulty"] is not None
    }
    return fight_windows, pull_numbers


def get_events(token, report_code, start_time, end_time):
    query = """
    query($code: String!, $startTime: Float!, $endTime: Float!) {
      reportData {
        report(code: $code) {
          events(
            startTime: $startTime
            endTime: $endTime
            dataType: Buffs
            hostilityType: Friendlies
            limit: 10000
          ) {
            data
            nextPageTimestamp
          }
        }
      }
    }
    """
    events = []
    while True:
        data = gql(token, query, {"code": report_code, "startTime": start_time, "endTime": end_time})
        page = data["reportData"]["report"]["events"]
        events.extend(page["data"])
        if page["nextPageTimestamp"]:
            start_time = page["nextPageTimestamp"]
        else:
            break
    return events


def get_master_data(token, report_code):
    query = """
    query($code: String!) {
      reportData {
        report(code: $code) {
          masterData {
            actors { id name }
            abilities { gameID name }
          }
        }
      }
    }
    """
    data = gql(token, query, {"code": report_code})
    master = data["reportData"]["report"]["masterData"]
    actor_names = {a["id"]: a["name"] for a in master["actors"]}
    ability_names = {a["gameID"]: a["name"] for a in master["abilities"]}
    return actor_names, ability_names


def fmt_time(ms):
    total_seconds = ms / 1000
    minutes = int(total_seconds // 60)
    seconds = total_seconds - minutes * 60
    return f"{minutes}:{seconds:06.3f}"


def find_hits(events):
    removals = [e for e in events if e.get("type") == "removebuff"]
    by_target_time = {}
    by_ability = {}
    for e in removals:
        key = (e.get("targetID"), e["timestamp"])
        by_target_time.setdefault(key, []).append(e)
        by_ability.setdefault(e["abilityGameID"], []).append(e)

    hits = []
    for (target_id, ts), group in by_target_time.items():
        ability_ids = {e["abilityGameID"] for e in group}
        if SPIRITFONT_ID not in ability_ids or len(ability_ids) <= 1:
            continue
        if len(group) >= MASS_REMOVAL_THRESHOLD:
            continue
        if ability_ids & EXCLUDED_ABILITY_IDS:
            continue

        other_abilities = ability_ids - {SPIRITFONT_ID}
        # global removals are like roar from druid removing off everyone at the same or similar timestamps
        looks_global = False
        for aid in other_abilities:
            for other_e in by_ability.get(aid, []):
                if other_e.get("targetID") == target_id:
                    continue
                if abs(other_e["timestamp"] - ts) <= NEARBY_WINDOW_MS:
                    looks_global = True
                    break
            if looks_global:
                break
        if looks_global:
            continue

        hits.append((target_id, ts, group))
    return hits


def pull_label_for(fight_id, pull_numbers):
    pull_number = pull_numbers.get(fight_id)
    if pull_number is not None:
        return f"pull {pull_number} [id {fight_id}]"
    return f"fight {fight_id}"


def event_link(report_code, fight_id, ts, window_ms=5000):
    return f"https://www.warcraftlogs.com/reports/{report_code}?fight={fight_id}&type=summary&view=events&start={ts}&end={ts + window_ms}"


def out_file_path(report_code, fight_id=None):
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    os.makedirs(out_dir, exist_ok=True)
    name = f"{report_code}_{fight_id}.out" if fight_id is not None else f"{report_code}.out"
    return os.path.join(out_dir, name)


def format_hit(report_code, fight_id, start_time, target_id, ts, group, actor_names, ability_names, prefix=""):
    rel_ts = ts - start_time
    target_name = actor_names.get(target_id, f"id:{target_id}")
    abilities = ", ".join(
        ability_names.get(e["abilityGameID"], f"id:{e['abilityGameID']}") for e in group
    )
    link = event_link(report_code, fight_id, ts)
    return f"{prefix}[{fmt_time(rel_ts)}]  {target_name}  ->  {abilities}\n{prefix}  {link}"


def fetch_all_hits(token, report_code, fight_ids):
    fight_windows, _ = get_fight_data(token, report_code)

    def fetch(fight_id):
        start_time, end_time = fight_windows[fight_id]
        events = get_events(token, report_code, start_time, end_time)
        return fight_id, start_time, find_hits(events)

    fight_hits = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch, fight_id) for fight_id in fight_ids]
        for future in as_completed(futures):
            fight_id, start_time, hits = future.result()
            fight_hits[fight_id] = (start_time, hits)
    return fight_hits


def run(token, report_code, fight_id=None):
    with Spinner("fetching fight data..."):
        fight_windows, pull_numbers = get_fight_data(token, report_code)
        if fight_id is not None and fight_id not in fight_windows:
            raise RuntimeError(f"fight {fight_id} not found in report {report_code}")
        actor_names, ability_names = get_master_data(token, report_code)

        fight_ids = [fight_id] if fight_id is not None else list(fight_windows)
        fight_hits = fetch_all_hits(token, report_code, fight_ids)

    colliding_ability_counter = Counter()
    total_hits = 0
    detail_lines = []
    for fight_index, fid in enumerate(fight_ids):
        start_time, hits = fight_hits[fid]
        for target_id, ts, group in sorted(hits, key=lambda h: h[1]):
            total_hits += 1
            other_abilities = [e["abilityGameID"] for e in group if e["abilityGameID"] != SPIRITFONT_ID]
            for aid in other_abilities:
                colliding_ability_counter[ability_names.get(aid, f"id:{aid}")] += 1

            prefix = "" if fight_id is not None else f"  {pull_label_for(fid, pull_numbers)} "
            line = format_hit(report_code, fid, start_time, target_id, ts, group, actor_names, ability_names, prefix)
            detail_lines.append(((fight_index, ts), line))

    detail_lines.sort(key=lambda x: x[0])

    out_path = out_file_path(report_code, fight_id)
    with open(out_path, "w") as out_f, redirect_stdout(Tee(sys.stdout, out_f)):
        if fight_id is not None:
            pull_label = pull_label_for(fight_id, pull_numbers)
            if not total_hits:
                print(f"no simultaneous removals found ({pull_label})")
            else:
                print(f"found {total_hits} simultaneous removals ({pull_label}):\n")
                for _, line in detail_lines:
                    print(line)
        else:
            print(f"total qualifying simultaneous-removal hits across {len(fight_windows)} fights: {total_hits}\n")
            print("colliding ability counts:")
            for name, count in colliding_ability_counter.most_common():
                print(f"  {count:3d}  {name}")

            print("\ndetail:")
            for _, line in detail_lines:
                print(line)
    print(f"\nwrote output to {out_path}")


def main():
    if len(sys.argv) not in (2, 3):
        print("usage: python spiritfont_removal.py <report_code> [fight_id]")
        print("where [fight_id] is optional, and will run an aggregate if not specified")
        sys.exit(1)

    report_code = sys.argv[1]
    token = get_token()
    fight_id = int(sys.argv[2]) if len(sys.argv) == 3 else None
    run(token, report_code, fight_id)


if __name__ == "__main__":
    main()
