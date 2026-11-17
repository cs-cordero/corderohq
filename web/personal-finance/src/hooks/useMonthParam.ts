import { useCallback } from "react";
import { useSearchParams } from "react-router";
import { currentYearMonth } from "../format";

const MONTH_PARAM = "month";
const YEAR_MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;

// Return the month of `?month=` if it is a valid YYYY-MM value, else null.
export function readMonthParam(params: URLSearchParams): string | null {
    const raw = params.get(MONTH_PARAM);
    return raw !== null && YEAR_MONTH.test(raw) ? raw : null;
}

// The month of the page, from `?month=YYYY-MM`. With no valid value, it is the current month in ET.
// A month change pushes a history entry, so Back goes to the month before, and the leave guard sees it.
export function useMonthParam(): [string, (month: string) => void] {
    const [params, setParams] = useSearchParams();
    const month = readMonthParam(params) ?? currentYearMonth();
    const setMonth = useCallback(
        (next: string) => {
            setParams((previous) => {
                const updated = new URLSearchParams(previous);
                updated.set(MONTH_PARAM, next);
                return updated;
            });
        },
        [setParams],
    );
    return [month, setMonth];
}
