// The browser tab names the page, so a row of dashboard tabs stays tellable apart.
import { useEffect } from "react";

export const APP_TITLE = "forge dashboard";

export function usePageTitle(title: string | null): void {
  useEffect(() => {
    document.title = title ? `${title} · ${APP_TITLE}` : APP_TITLE;
  }, [title]);
}
