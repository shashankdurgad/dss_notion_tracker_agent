interface Props {
  authError?: string | null;
}

const ERROR_COPY: Record<string, string> = {
  bad_state:
    "That sign-in link was stale or already used. Please try again.",
  missing_params: "Notion sent back an incomplete response. Please try again.",
  exchange_failed:
    "Couldn't complete sign-in with Notion. Please try again.",
  access_denied: "Sign-in was cancelled.",
};

export default function LoginGate({ authError }: Props) {
  return (
    <div className="gate">
      <div className="gate-card">
        <h1>DSS Notion Assistant</h1>
        <p className="gate-sub">
          Ask questions about UCL Data Science Society's Notion workspace —
          committee minutes, events, sponsors and action items.
        </p>

        {authError && (
          <p className="gate-error" role="alert">
            {ERROR_COPY[authError] ?? "Sign-in failed. Please try again."}
          </p>
        )}

        <a className="btn-primary" href="/auth/login">
          Connect Notion
        </a>

        <p className="gate-note">
          You'll sign in with your own Notion account. The assistant can only
          see pages you already have access to, and it asks before making any
          change.
        </p>
      </div>
    </div>
  );
}
