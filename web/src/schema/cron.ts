// A plain-language reading of five-field cron expressions, for the cron widget.
// It describes common shapes and validates field syntax; the server's croniter
// remains the authority on what is accepted.

const FIELD = /^(\*|\d+(-\d+)?)(\/\d+)?(,(\*|\d+(-\d+)?)(\/\d+)?)*$/;
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
// Inclusive bounds per field: minute, hour, day of month, month, day of week (7 is Sunday too).
const BOUNDS: [number, number][] = [
  [0, 59],
  [0, 23],
  [1, 31],
  [1, 12],
  [0, 7],
];

/** Every list item's values fall inside the field's bounds; a step ("/15") only has to be positive. */
function inRange(part: string, [low, high]: [number, number]): boolean {
  return part.split(",").every((item) => {
    const [range = "", step] = item.split("/");
    if (step !== undefined && Number(step) < 1) return false;
    if (range === "*") return true;
    const [first, last = first] = range.split("-").map(Number);
    return (
      first !== undefined && last !== undefined && low <= first && first <= last && last <= high
    );
  });
}

function pad(value: string): string {
  return value.padStart(2, "0");
}

/** A sentence for the expression, or null when it is not five well-formed fields. */
export function describeCron(expression: string): string | null {
  const parts = expression.trim().split(/\s+/);
  if (parts.length !== 5 || !parts.every((part) => FIELD.test(part))) return null;
  if (!parts.every((part, index) => inRange(part, BOUNDS[index] ?? [0, 0]))) return null;
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
