"""The Sunday job: plan next week, write the calendar file, rebuild the report, and (if
you've signed in to Google) update your Training calendar, including the "Week plan"
summary event. Data arrives continuously through receiver.py, so there's nothing to sync here."""
import coach
import report

if __name__ == "__main__":
    try:
        coach.main([])      # also writes training.ics
    except Exception as e:
        coach.notify(f"This week's plan failed: {type(e).__name__}. See ~/.fitness-coach/weekly.log")
        raise
    try:
        report.build()
    except Exception as e:  # the plan and calendar are already saved; keep going to Google
        print(f"Report failed: {type(e).__name__}: {e}", flush=True)
        coach.notify("The plan is ready, but the report failed to build. See ~/.fitness-coach/weekly.log")

    import google_api
    if google_api.configured():
        import gcal
        try:
            gcal.sync()
        except Exception as e:
            print(f"Google Calendar failed: {type(e).__name__}: {e}", flush=True)
            coach.notify("The plan is ready, but updating Google Calendar failed. See weekly.log")
            raise SystemExit(1)
