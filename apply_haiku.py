"""
apply_haiku.py — CLI helper for the /apply-jobs --haiku skill.

Commands:
  list   [--limit N] [--type TYPE]   Print JSON array of pending jobs.
  mark   <job_id> <applied|skipped|failed>   Update DB status.
  stats                               Print JSON counts.

Called by the Claude Code skill; not intended for direct use.
"""
import argparse, json, os, sqlite3, sys

from dotenv import load_dotenv
load_dotenv()

_DB = os.path.abspath(os.environ.get("DB_PATH", "linkedin_jobs.db"))

_STATUS = {"applied": 1, "skipped": -1, "failed": -2}


def _conn():
    return sqlite3.connect(_DB)


def cmd_list(limit: int, job_type: str | None):
    conn = _conn()
    q = """
        SELECT job_id, title, application_type, application_url,
               posting_domain, location, formatted_experience_level,
               description, remote_allowed
        FROM jobs
        WHERE scraped > 0 AND applied IS NULL
          AND (
              remote_allowed = 1
              OR location LIKE '%Utah%'
              OR location LIKE '%Remote%'
              OR location LIKE '%United States%'
              OR location IS NULL OR location = ''
          )
    """
    params: list = []
    if job_type:
        types = [t.strip() for t in job_type.split(",")]
        q += f" AND application_type IN ({','.join('?'*len(types))})"
        params.extend(types)
    q += " ORDER BY rowid ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    jobs = [
        {
            "job_id":           r[0],
            "title":            r[1] or "",
            "application_type": r[2] or "",
            "application_url":  r[3] or "",
            "posting_domain":   r[4] or "",
            "location":         r[5] or "",
            "level":            r[6] or "",
            "description":      (r[7] or "")[:3000],
            "remote":           bool(r[8]),
        }
        for r in rows
    ]
    print(json.dumps(jobs, indent=2))


def cmd_mark(job_id: str, status: str):
    code = _STATUS.get(status)
    if code is None:
        sys.exit(f"Unknown status '{status}'. Use: applied | skipped | failed")
    conn = _conn()
    conn.execute("UPDATE jobs SET applied=? WHERE job_id=?", (code, job_id))
    conn.commit()
    conn.close()
    print(f"Marked {job_id} → {status} ({code})")


def cmd_stats():
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM jobs WHERE scraped>0 AND applied IS NULL")
    pending = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE applied=1")
    applied = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE applied=-1")
    skipped = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM jobs WHERE applied=-2")
    failed = c.fetchone()[0]
    conn.close()
    print(json.dumps({"pending": pending, "applied": applied,
                      "skipped": skipped, "failed": failed}))


def main():
    p = argparse.ArgumentParser(description="Haiku apply helper")
    sub = p.add_subparsers(dest="cmd")

    lst = sub.add_parser("list", help="List pending jobs as JSON")
    lst.add_argument("--limit", type=int, default=10)
    lst.add_argument("--type", default=None, metavar="TYPE",
                     help="Comma-separated application types to filter")

    mk = sub.add_parser("mark", help="Update job status in DB")
    mk.add_argument("job_id")
    mk.add_argument("status", choices=list(_STATUS))

    sub.add_parser("stats", help="Print job counts as JSON")

    args = p.parse_args()
    if args.cmd == "list":
        cmd_list(args.limit, args.type)
    elif args.cmd == "mark":
        cmd_mark(args.job_id, args.status)
    elif args.cmd == "stats":
        cmd_stats()
    else:
        p.print_help()


if __name__ == "__main__":
    main()
