// Fixture builders and routing helpers for the settings page.
import type { Schemas } from "@/api/client";
import { NOW_SECONDS } from "./constants";
import type { MockApi } from "./harness";

export type BackupInfo = Schemas["BackupInfo"];

export const DISCOVERY_CONFIG: Schemas["DiscoveryConfig"] = {
  roots: ["C:/code"],
  max_depth: 6,
  rescan_hours: 24,
  extra_projects: [],
  ignored: [],
  forge_repo: null,
};

/** Route GET and PUT for one service's config; PUT echoes the body unless `saved` says otherwise. */
export function routeConfig(
  api: MockApi,
  service: string,
  config: Record<string, unknown>,
  saved?: (body: unknown) => Record<string, unknown> | Promise<Record<string, unknown>>,
): MockApi {
  return api
    .config(service, config)
    .route("PUT", `/api/services/${service}/config`, (body: unknown) =>
      saved ? saved(body) : (body as Record<string, unknown>),
    );
}

export function makeBackup(name: string, overrides: Partial<BackupInfo> = {}): BackupInfo {
  return {
    name,
    path: `C:/data/backups/${name}`,
    size_bytes: 2048,
    created_at: NOW_SECONDS - 3600,
    ...overrides,
  };
}

export function makeGitHubState(
  overrides: Partial<Schemas["GitHubState"]> = {},
): Schemas["GitHubState"] {
  return {
    auth_source: "none",
    authenticated: false,
    rate_limit: null,
    rate_remaining: null,
    rate_reset_at: null,
    repos: [],
    ...overrides,
  };
}

export function makeGalleryState(
  overrides: Partial<Schemas["GalleryState"]> = {},
): Schemas["GalleryState"] {
  return {
    updated_at: NOW_SECONDS - 60,
    elapsed_s: 3725,
    size: 5368709120,
    ratio: 87.5,
    progress: 0.6,
    status: "warn",
    commit: "1a2b3c4",
    link: "https://example.com",
    location: "C:/projects/demo",
    notes: "**Markdown** with a [link](https://example.com) and `code`.",
    marker: "active",
    pings: 0,
    ...overrides,
  };
}
