from datetime import datetime, timezone
import uuid

import app


def main():
    settings = app.load_settings()
    app.agent_state["running"] = True
    app.agent_state["mode"] = settings.get("mode", "live")
    app.agent_state["started_at"] = datetime.now(timezone.utc).isoformat()
    app.agent_state["run_id"] = str(uuid.uuid4())[:8]
    app.agent_state["current_job"] = None

    print(
        {
            "run_id": app.agent_state["run_id"],
            "mode": app.agent_state["mode"],
            "job_source": settings.get("job_source"),
            "preferred_locations": settings.get("preferred_locations"),
        }
    )
    app.run_agent()


if __name__ == "__main__":
    main()
