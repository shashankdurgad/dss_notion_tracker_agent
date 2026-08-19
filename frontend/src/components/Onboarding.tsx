import { SERVICES } from "../services";
import type { AuthStatus } from "../types";

interface Props {
  status: AuthStatus;
  authError?: string | null;
  errorService?: string | null;
}

const ERROR_COPY: Record<string, string> = {
  bad_state: "That link was stale or already used. Please try connecting again.",
  missing_params: "The service sent back an incomplete response. Try again.",
  exchange_failed: "Couldn't complete the connection. Please try again.",
  access_denied: "You cancelled the connection.",
  session_expired: "Your session expired. Connect Notion again first.",
};

export default function Onboarding({ status, authError, errorService }: Props) {
  // Steps are ordered; the first unconnected one is where the user is now, so
  // refreshing mid-flow resumes rather than restarting.
  const steps = SERVICES.filter((s) => status.connections[s.id]?.available !== false);
  const activeIndex = steps.findIndex((s) => !status.connections[s.id]?.connected);
  const done = activeIndex === -1;
  const active = done ? null : steps[activeIndex];

  return (
    <div className="gate">
      <div className="gate-card onboarding">
        <h1>DSS Assistant</h1>
        <p className="gate-sub">
          Ask questions across the society's Notion workspace and tracker
          spreadsheets — and update them, with your approval.
        </p>

        <ol className="steps" aria-label="Setup progress">
          {steps.map((service, i) => {
            const connected = status.connections[service.id]?.connected;
            const isActive = i === activeIndex;
            return (
              <li
                key={service.id}
                className={
                  connected ? "step step-done" : isActive ? "step step-active" : "step"
                }
                aria-current={isActive ? "step" : undefined}
              >
                <span className="step-mark" aria-hidden="true">
                  {connected ? "✓" : i + 1}
                </span>
                <span className="step-name">{service.name}</span>
                {connected && (
                  <span className="step-status">
                    {status.connections[service.id]?.account ?? "Connected"}
                  </span>
                )}
              </li>
            );
          })}
        </ol>

        {authError && (
          <p className="gate-error" role="alert">
            {ERROR_COPY[authError] ?? "Something went wrong. Please try again."}
            {errorService ? ` (${errorService})` : ""}
          </p>
        )}

        {active ? (
          <div className="step-detail">
            <h2>
              {active.icon} Connect {active.name}
            </h2>
            <p>{active.blurb}</p>
            {active.caution && <p className="step-caution">{active.caution}</p>}
            <a className="btn-primary" href={active.connectPath}>
              Connect {active.name}
            </a>
            <p className="gate-note">
              Step {activeIndex + 1} of {steps.length} · both are needed before
              you can start.
            </p>
          </div>
        ) : (
          <div className="step-detail">
            <h2>✅ All set</h2>
            <p>Everything's connected. Loading your assistant…</p>
          </div>
        )}
      </div>
    </div>
  );
}
