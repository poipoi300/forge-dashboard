import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Toaster } from "sonner";
import { startStream } from "@/api/stream";
import { TooltipProvider } from "@/components/ui/tooltip";
import { startClock } from "@/lib/time";
import { App } from "./app/App";
import { applyPreferences } from "./app/preferences";

const container = document.getElementById("root");
if (!container) throw new Error("index.html is missing #root");

applyPreferences();
startClock();
startStream();

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5_000 } },
});

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={300}>
        <App />
        <Toaster
          position="bottom-right"
          closeButton
          toastOptions={{ className: "font-sans text-sm" }}
        />
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
);
