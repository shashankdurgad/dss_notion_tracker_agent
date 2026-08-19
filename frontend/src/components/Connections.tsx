import { useState } from "react";
import { SERVICES } from "../services";
import type { AuthStatus, ServiceId } from "../types";

interface Props {
  status: AuthStatus;
  onDisconnect: (service: ServiceId) => void;
}

export default function Connections({ status, onDisconnect }: Props) {
  const [confirming, setConfirming] = useState<ServiceId | null>(null);

  return (
    <div className="connections">
      <div className="connections-label">Connected services</div>

      {SERVICES.map((service) => {
        const state = status.connections[service.id];
        if (state?.available === false) return null;

        return (
          <div key={service.id} className="connection">
            <span className="connection-icon" aria-hidden="true">
              {service.icon}
            </span>
            <span className="connection-body">
              <span className="connection-name">{service.name}</span>
              <span className="connection-status">
                {state?.connected
                  ? (state.account ?? "Connected")
                  : "Not connected"}
              </span>
            </span>

            {state?.connected ? (
              confirming === service.id ? (
                <span className="chat-confirm">
                  <button
                    onClick={() => {
                      onDisconnect(service.id);
                      setConfirming(null);
                    }}
                  >
                    Disconnect
                  </button>
                  <button onClick={() => setConfirming(null)}>Cancel</button>
                </span>
              ) : (
                <button
                  className="connection-action"
                  onClick={() => setConfirming(service.id)}
                  // Disconnecting Notion ends the session, since it carries
                  // the app's identity — say so rather than surprising them.
                  title={
                    service.id === "notion"
                      ? "Disconnecting Notion signs you out"
                      : `Disconnect ${service.name}`
                  }
                >
                  Disconnect
                </button>
              )
            ) : (
              <a className="connection-action" href={service.connectPath}>
                Connect
              </a>
            )}
          </div>
        );
      })}
    </div>
  );
}
