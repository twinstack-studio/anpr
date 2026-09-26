"""Demo society for the online demo: houses, residents, vehicles, passes, a simulated camera and two weeks of gate history.

Several plates that appear in the sample camera clip (samples/lahore-traffic.mp4) are registered here, so the live demo
shows residents, a service vehicle, a visitor with a pass and a blacklisted truck. Everything else it reads is unknown.
Reset: `.venv/bin/python -m app.society_demo --reset` (also run nightly by the server).
"""
import random
import shutil
import sys
from datetime import datetime, time, timedelta

from .society import DSN, PKT, SOC_DIR, hash_pw

DEMO_PASSWORD = "demo1234"
FIRST = ["Ahmed", "Ali", "Usman", "Bilal", "Hamza", "Saad", "Imran", "Kashif", "Faisal", "Naveed", "Tariq", "Zeeshan", "Omer", "Haris",
         "Asad", "Fahad", "Shoaib", "Adnan", "Waqas", "Junaid", "Ayesha", "Sana", "Fatima", "Hira", "Maryam", "Nadia", "Rabia", "Sadia",
         "Amna", "Zainab", "Mehwish", "Saima"]
LAST = ["Raza", "Khan", "Malik", "Butt", "Chaudhry", "Sheikh", "Qureshi", "Iqbal", "Siddiqui", "Mirza", "Aslam", "Javed", "Hussain",
        "Akhtar", "Rana", "Anwar", "Bhatti", "Awan", "Hashmi", "Gill"]
MAKES = [("Toyota Corolla", "Car"), ("Honda Civic", "Car"), ("Suzuki Alto", "Car"), ("Suzuki Cultus", "Car"), ("Toyota Yaris", "Car"),
         ("Honda City", "Car"), ("KIA Sportage", "Car"), ("Hyundai Tucson", "Car"), ("Suzuki Wagon R", "Car"), ("Toyota Fortuner", "Car"),
         ("MG HS", "Car"), ("Changan Alsvin", "Car"), ("Honda CD 70", "Bike"), ("Honda CG 125", "Bike"), ("Yamaha YBR", "Bike")]
COLOURS = ["White", "Black", "Silver", "Grey", "Red", "Blue", "Maroon"]
PURPOSES = ["Family visit", "Guests for dinner", "Delivery", "Plumber", "Electrician", "Tuition teacher", "Friend", "Driver",
            "AC service", "Doctor", "Relatives", "Cable / internet repair"]

# plates read in the sample clip -> role in the demo
CLIP = {
    "LEA-20-4060": ("resident", "A", "12", "Toyota Corolla", "Car", "White"),
    "LE-16-5471A": ("resident", "B", "4", "Suzuki Cultus", "Car", "Silver"),
    "AAN-267": ("resident", "C", "21", "Honda City", "Car", "Grey"),
    "LEN-5128": ("resident", "A", "7", "Honda CD 70", "Bike", "Red"),
    "LEM-7190": ("resident", "B", "15", "Honda CG 125", "Bike", "Black"),
    "LEQ-7234B": ("resident", "C", "3", "Yamaha YBR", "Bike", "Blue"),
    "LEN-8209": ("resident", "A", "18", "Honda CD 70", "Bike", "Red"),
    "LES-5033": ("service", None, None, "Water tanker (Al-Madina Water)", "Truck", "Blue"),
}
CLIP_VISITOR = "AZF-52"
CLIP_BLACKLIST = ("LEB-3789", "Stolen vehicle, FIR no. 1142/26 Model Town")


def rand_plate(rng: random.Random) -> str:
    kind = rng.random()
    if kind < 0.6:
        return f"LE{rng.choice('ABCDFGHJKMNPRSTVWXZ')}-{rng.randint(1000, 9999)}"
    if kind < 0.75:
        return f"LE{rng.choice('ABCDFGHJKMNPRSTVWXZ')}-{rng.choice(['18', '19', '20', '21', '22', '23', '24'])}-{rng.randint(1000, 9999)}"
    if kind < 0.85:
        return f"{rng.choice(['A', 'B', 'C', 'R', 'S'])}{rng.choice('BCDEFHKLMNPRSTWXZ')}{rng.choice('ABCDEFHJKLMNPRSTWXZ')}-{rng.randint(100, 999)}"
    if kind < 0.93:
        return f"B{rng.choice('ABCDEFHJKLMNPRS')}{rng.choice('ABCDEFHJKLMNPRS')}-{rng.randint(100, 999)}"
    return f"{rng.choice(['RIM', 'RIN', 'FDA', 'FDB', 'GAA', 'MNA'])}-{rng.randint(1000, 9999)}"


def seed(c):
    rng = random.Random(7)
    today = datetime.now(PKT).date()
    now = datetime.now(PKT)
    # the demo camera is a busy street, not a gate lane: skip plate-less bikes and repeat loops of the clip
    for k, val in (("society_name", "Green Valley Residencia"), ("log_unreadable", "0"), ("dedupe_minutes", "30")):
        c.execute("insert into soc_settings (key, value) values (%s,%s) on conflict do nothing", (k, val))

    # houses: blocks A-C, 30 each
    houses = {}
    for block in "ABC":
        for n in range(1, 31):
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            status = "vacant" if rng.random() < 0.06 else "tenant" if rng.random() < 0.25 else "owner"
            phone = f"03{rng.randint(0, 4)}{rng.randint(0, 9)}-{rng.randint(1000000, 9999999)}"
            hid = c.execute("insert into soc_houses (block, number, owner, phone, status) values (%s,%s,%s,%s,%s) returning id",
                            (block, str(n), name, phone, status)).fetchone()["id"]
            houses[(block, str(n))] = {"id": hid, "owner": name, "status": status}

    # vehicles
    plates = {}  # plate -> (house_id, vehicle type)
    for plate, (kind, block, num, make, vtype, colour) in CLIP.items():
        hid = houses[(block, num)]["id"] if block else None
        owner = houses[(block, num)]["owner"] if block else "Al-Madina Water Supply"
        c.execute("insert into soc_vehicles (plate, kind, house_id, vehicle, make, colour, owner_name) values (%s,%s,%s,%s,%s,%s,%s)",
                  (plate, kind, hid, vtype, make, colour, owner))
        if hid:
            plates[plate] = (hid, vtype)
    for (block, num), h in houses.items():
        if h["status"] == "vacant":
            continue
        for _ in range(rng.choice([1, 1, 2, 2, 2, 3])):
            plate = rand_plate(rng)
            if plate in plates or plate in CLIP:
                continue
            make, vtype = rng.choice(MAKES)
            c.execute("insert into soc_vehicles (plate, kind, house_id, vehicle, make, colour, owner_name) values (%s,'resident',%s,%s,%s,%s,%s)",
                      (plate, h["id"], vtype, make, rng.choice(COLOURS), h["owner"]))
            plates[plate] = (h["id"], vtype)
    service = [("LET-6621", "Milk supplier (Haleeb)", "Van"), ("LEH-3410", "Garbage collection (LWMC)", "Truck"),
               ("LEF-9021", "Society maintenance van", "Van")]
    for plate, what, vtype in service:
        c.execute("insert into soc_vehicles (plate, kind, vehicle, make, owner_name) values (%s,'service',%s,%s,%s)", (plate, vtype, what, what))
    staff = [("LEW-4417", "Society manager (Kamran Shah)", "Car"), ("LEP-1180", "Security supervisor (Rafaqat Ali)", "Bike")]
    for plate, who, vtype in staff:
        c.execute("insert into soc_vehicles (plate, kind, vehicle, owner_name) values (%s,'staff',%s,%s)", (plate, vtype, who))

    # blacklist
    c.execute("insert into soc_blacklist (plate, reason) values (%s,%s)", CLIP_BLACKLIST)
    c.execute("insert into soc_blacklist (plate, reason) values ('LEX-2290', 'Banned by management: harassment complaint, 2 Aug')")
    c.execute("insert into soc_blacklist (plate, reason) values ('BKR-418', 'Unpaid contractor, entry not allowed')")

    # accounts
    a12 = houses[("A", "12")]
    users = [("admin", "Management Office", "admin", None), ("guard", "Main Gate Guard", "guard", None),
             ("resident", a12["owner"], "resident", a12["id"])]
    ids = {}
    for username, name, role, hid in users:
        ids[username] = c.execute("insert into soc_users (username, name, password, role, house_id, locked) values (%s,%s,%s,%s,%s,true) returning id",
                                  (username, name, hash_pw(DEMO_PASSWORD), role, hid)).fetchone()["id"]

    # passes
    hid_list = [h["id"] for h in houses.values() if h["status"] != "vacant"]

    def add_pass(house_id, visitor, plate, vf, vt, purpose, uses=0, max_entries=1, by=None):
        code = "".join(rng.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6))
        return c.execute("""insert into soc_passes (code, house_id, visitor, phone, plate, purpose, valid_from, valid_to, max_entries, uses, created_by)
                            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                         (code, house_id, visitor, f"03{rng.randint(0, 4)}{rng.randint(0, 9)}-{rng.randint(1000000, 9999999)}", plate, purpose,
                          vf, vt, max_entries, uses, by or ids["admin"])).fetchone()["id"]

    add_pass(a12["id"], "Kamran Raza (brother)", CLIP_VISITOR, today, today + timedelta(days=2), "Family visit", max_entries=5, by=ids["resident"])
    add_pass(a12["id"], "Ustad Aslam (plumber)", None, today, today, "Plumber", by=ids["resident"])
    for _ in range(9):
        vf = today + timedelta(days=rng.randint(0, 3))
        add_pass(rng.choice(hid_list), f"{rng.choice(FIRST)} {rng.choice(LAST)}", rand_plate(rng) if rng.random() < 0.6 else None,
                 vf, vf + timedelta(days=rng.choice([0, 0, 1])), rng.choice(PURPOSES))

    cam = c.execute("""insert into soc_cameras (name, gate, source, demo, barrier) values ('Main gate · Entry camera', 'entry', 'demo:gate-demo.mp4', true, 'sim')
                       returning id""").fetchone()["id"]

    # two weeks of history (no photos; the camera adds real ones)
    events = []
    res_plates = list(plates.items())
    for d in range(14, -1, -1):
        day = today - timedelta(days=d)

        def at(h0, h1):
            t = datetime.combine(day, time(0), PKT) + timedelta(minutes=rng.randint(int(h0 * 60), int(h1 * 60)))
            return t

        for plate, (hid, vtype) in res_plates:
            if rng.random() < 0.55:
                out = at(7, 10.5) if rng.random() < 0.7 else at(10, 20)
                back = out + timedelta(minutes=rng.randint(40, 600))
                events.append((out, "exit", plate, vtype, "resident", "allowed", hid, None, None, ""))
                events.append((back, "entry", plate, vtype, "resident", "allowed", hid, None, None, ""))
        for plate, what, vtype in service:
            if rng.random() < 0.8:
                t = at(6, 11)
                events.append((t, "entry", plate, vtype, "service", "allowed", None, None, None, what))
                events.append((t + timedelta(minutes=rng.randint(15, 70)), "exit", plate, vtype, "service", "allowed", None, None, None, what))
        for plate, who, vtype in staff:
            t = at(8.5, 9.5)
            events.append((t, "entry", plate, vtype, "staff", "allowed", None, None, None, who))
            events.append((t + timedelta(hours=rng.uniform(8, 9.5)), "exit", plate, vtype, "staff", "allowed", None, None, None, who))
        for _ in range(rng.randint(12, 22)):  # visitors with passes
            hid = rng.choice(hid_list)
            plate = rand_plate(rng)
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            purpose = rng.choice(PURPOSES)
            pid = add_pass(hid, name, plate, day, day, purpose, uses=1)
            t = at(10, 21.5)
            events.append((t, "entry", plate, rng.choice(["Car", "Car", "Bike"]), "visitor", "allowed", hid, pid, name, purpose))
            events.append((t + timedelta(minutes=rng.randint(20, 200)), "exit", plate, "Car", "visitor", "allowed", hid, pid, name, ""))
        for _ in range(rng.randint(3, 7)):  # unknown vehicles: guard called the house
            hid = rng.choice(hid_list)
            plate = rand_plate(rng)
            t = at(9, 22)
            ok = rng.random() < 0.8
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            events.append((t, "entry", plate, "Car", "visitor" if ok else "unknown", "allowed" if ok else "denied", hid if ok else None, None,
                           name if ok else None, "Guard called the house" if ok else "Resident did not confirm"))
            if ok:
                events.append((t + timedelta(minutes=rng.randint(15, 120)), "exit", plate, "Car", "visitor", "allowed", hid, None, name, ""))
        if rng.random() < 0.25:
            t = at(11, 23)
            events.append((t, "entry", "LEX-2290", "Car", "blacklist", "denied", None, None, None, "Banned by management: harassment complaint, 2 Aug"))
        for _ in range(rng.randint(1, 3)):
            t = at(8, 22)
            events.append((t, "entry", None, rng.choice(["Bike", "Car"]), "unreadable", "no_action", None, None, None, ""))

    guard = ids["guard"]
    rows = [e for e in events if e[0] <= now - timedelta(minutes=10)]
    # a pending unknown right now, so the guard screen has something to decide
    rows.append((now - timedelta(minutes=3), "entry", "LEK-7722", "Bike", "unknown", "pending", None, None, None, ""))
    with c.cursor() as cur:
        cur.executemany("""insert into soc_events (at, gate, source, plate, vehicle, category, status, house_id, pass_id, visitor, note,
                           decided_by, decided_at, demo) values (%s,%s,'camera',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)""",
                        [(t, g, p, v, cat, st, h, pid, vis, note, guard if cat in ("visitor", "unknown") and st != "pending" and not pid else None,
                          t + timedelta(seconds=40) if cat in ("visitor", "unknown") and not pid else None) for t, g, p, v, cat, st, h, pid, vis, note in rows])
    return cam


def reset():
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as c:
        c.execute("""truncate soc_push, soc_requests, soc_barrier_log, soc_events, soc_passes, soc_blacklist, soc_vehicles, soc_sessions, soc_users, soc_cameras, soc_houses,
                     soc_settings restart identity cascade""")
        seed(c)
    for p in SOC_DIR.iterdir():
        if p.is_dir() and p.name != "tmp":
            shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__" and "--reset" in sys.argv:
    reset()
    print("demo society reset")
