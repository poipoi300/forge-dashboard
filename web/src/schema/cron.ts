// A plain-language reading of five-field cron expressions, for the cron widget.
// It describes common shapes and validates field syntax; the server's croniter
// remains the authority on what is accepted.

const FIELD = /^(\*|\d+(-\d+)?)(\/\d+)?(,(\*|\d+(-\d+)?)(\/\d+)?)*$/;
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function pad(value: string): string {
  return value.padStart(2, "0");
}

/** A sentence for the expression, or null when it is not five well-formed fields. */
export function describeCron(expression: string): string | null {
  const parts = expression.trim().split(/\s+/);
  if (parts.length !== 5 || !parts.every((part) => FIELD.test(part))) return null;
  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts as [
    string,
    string,
    string,
    string,
    string,
  ];
  const everyDay = dayOfMonth === "*" && month === "*" && dayOfWeek === "*";
  if (minute.startsWith("*/") && hour === "*" && everyDay)
    return `Every ${minute.slice(2)} minutes`;
  if (/^\d+$/.test(minute) && hour === "*" && everyDay) return `Every hour at :${pad(minute)}`;
  if (/^\d+$/.test(minute) && hour.startsWith("*/") && everyDay) {
    return `Every ${hour.slice(2)} hours at :${pad(minute)}`;
  }
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour)) {
    const time = `${pad(hour)}:${pad(minute)}`;
    if (everyDay) return `Every day at ${time}`;
    if (dayOfMonth === "*" && month === "*" && /^\d$/.test(dayOfWeek)) {
      return `Every ${DAYS[Number(dayOfWeek) % 7]} at ${time}`;
    }
    if (dayOfMonth === "*" && month === "*" && dayOfWeek === "1-5") return `Weekdays at ${time}`;
    if (/^\d+$/.test(dayOfMonth) && month === "*" && dayOfWeek === "*") {
      return `Day ${dayOfMonth} of every month at ${time}`;
    }
  }
  return `Custom schedule: ${expression.trim()}`;
}
