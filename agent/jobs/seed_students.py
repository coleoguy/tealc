"""One-shot seeder for the students table.

Run once (idempotent — safe to run again; uses INSERT OR IGNORE):
    python -m agent.jobs.seed_students
"""
import sqlite3
from agent.scheduler import DB_PATH


STUDENTS = [
    # (full_name, short_name, role, joined_iso, primary_project)
    ("Sean Chien",              "Sean",     "PhD",     "2022-01-01", "evolutionary biology, Coleoptera"),
    ("Megan Copeland",          "Megan",    "PhD",     "2022-01-01", "genome structure, genomics, bioinformatics"),
    ("Andres Barboza Pereira",  "Andres",   "PhD",     "2023-01-01", "theoretical evolution, population genetics, Chondrichthyes"),
    ("Kaya Harper",             "Kaya",     "PhD",     "2023-01-01", "environmental variation, genomic architecture, epigenetics"),
    ("Shelbie Cast",            "Shelbie",  "PhD",     "2025-01-01", "crustacean evolution, crab freshwater invasion"),
    ("Kiedon Bryant",           "Kiedon",   "PhD",     "2025-01-01", "behavioral ecology of fishes, mating systems"),
    ("Meghann McConnell",       "Meghann",  "PostBacc","2025-01-01", "chromosome number transitions, alternative meiosis"),
    ("LT Blackmon",             "LT",       "Staff",   None,         "fieldwork, morphometrics of Chrysina species"),
    ("Kenzie Laird",            "Kenzie",   "Staff",   None,         "model organism care, discrete trait PCMs, Betta fish aggression"),
    ("Bella Steele",            "Bella",    "UG",      None,         ""),
    ("Sarah Schmalz",           "Sarah",    "UG",      None,         ""),
    ("Emily Clark",             "Emily",    "UG",      None,         ""),
    ("Olivia Deiterman",        "Olivia",   "UG",      None,         ""),
    ("Riya Girish",             "Riya",     "UG",      None,         ""),
    ("Anna Klein",              "Anna",     "UG",      None,         ""),
    ("Mallory Murphy",          "Mallory",  "UG",      None,         ""),
    ("Alex Rathsack",           "Alex",     "UG",      None,         ""),
    ("Tewobola Olasehinde",     "Tewobola", "UG",      None,         ""),
    ("Gabe Rodriguez",          "Gabe",     "UG",      None,         ""),
    ("Carl Hjelmen",            "Carl",     "Alumni",  None,         "Asst Prof, Utah Valley"),
    ("Jamie Alfieri",           "Jamie",    "Alumni",  None,         "Postdoc, UT Austin"),
    ("Terrence Sylvester",      "Terrence", "Alumni",  None,         "Postdoc, UT Memphis"),
    ("Annabel Perry",           "Annabel",  "Alumni",  None,         "PhD, Harvard"),
    ("Max Chin",                "Max",      "Alumni",  None,         "PhD, UC Davis"),
    ("Kayla Wilhoit",           "Kayla",    "Alumni",  None,         "PhD, Duke"),
    ("Johnathan Lo",            "Johnathan","Alumni",  None,         "PhD, UC Berkeley"),
    ("Nathan Anderson",         "Nathan",   "Alumni",  None,         "PhD, UW Madison"),
]

# Default status per role
_STATUS = {
    "PhD": "active",
    "PostBacc": "active",
    "Staff": "active",
    "UG": "active",
    "Alumni": "graduated",
}


def seed():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    inserted = 0
    for row in STUDENTS:
        full_name, short_name, role, joined_iso, primary_project = row
        status = _STATUS.get(role, "active")
        cur = conn.execute(
            """INSERT OR IGNORE INTO students
               (full_name, short_name, role, joined_iso, status, primary_project)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (full_name, short_name, role, joined_iso, status, primary_project),
        )
        inserted += cur.rowcount
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    conn.close()
    print(f"Seeded {inserted} new student(s). Total in table: {total}")
    return total


if __name__ == "__main__":
    seed()
