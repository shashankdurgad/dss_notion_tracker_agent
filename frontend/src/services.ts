import type { ServiceId } from "./types";

export interface ServiceMeta {
  id: ServiceId;
  name: string;
  icon: string;
  /** Why the agent needs it — shown during onboarding. */
  blurb: string;
  connectPath: string;
  /** Extra warning shown before sending the user off to consent. */
  caution?: string;
}

export const SERVICES: ServiceMeta[] = [
  {
    id: "notion",
    name: "Notion",
    icon: "📄",
    blurb:
      "Committee minutes, event planning, sponsorship threads and action items. The assistant only sees pages you already have access to.",
    connectPath: "/auth/login",
  },
  {
    id: "sheets",
    name: "Google Sheets",
    icon: "📊",
    blurb:
      "Tracker spreadsheets — sponsor pipelines, budgets, attendance. The assistant can only open spreadsheets you pick, never your whole Drive.",
    connectPath: "/auth/google/login",
    // People reliably assume this screen means the app is broken, so name it
    // before they see it.
    caution:
      "Google will warn that this app isn't verified — that's expected for an internal society tool. Choose Advanced, then continue.",
  },
];

export function serviceMeta(id: ServiceId): ServiceMeta {
  return SERVICES.find((s) => s.id === id) ?? SERVICES[0];
}
