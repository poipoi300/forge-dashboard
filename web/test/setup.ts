import { GlobalRegistrator } from "@happy-dom/global-registrator";

GlobalRegistrator.register({ url: "http://localhost/" });
// The harness owns cleanup so it can close open action dialogs before the DOM is torn down.
process.env.RTL_SKIP_AUTO_CLEANUP = "true";
