import { useCallback, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "chq-theme";

// Return the stored theme. The default is light. A browser that blocks storage also gets light.
function readTheme(): Theme {
    try {
        return window.localStorage.getItem(STORAGE_KEY) === "dark" ? "dark" : "light";
    } catch {
        return "light";
    }
}

function applyTheme(theme: Theme): void {
    document.documentElement.dataset.theme = theme;
}

// Set `data-theme` on <html> from the stored theme. `main.tsx` calls this before React renders, so the
// page does not flash in the wrong theme.
export function applyStoredTheme(): void {
    applyTheme(readTheme());
}

// The current theme, and a function that changes it, stores it, and applies it at once.
export function useTheme(): [Theme, (theme: Theme) => void] {
    const [theme, setThemeState] = useState<Theme>(readTheme);
    const setTheme = useCallback((next: Theme) => {
        try {
            window.localStorage.setItem(STORAGE_KEY, next);
        } catch {
            // The theme then lasts only until a reload.
        }
        applyTheme(next);
        setThemeState(next);
    }, []);
    return [theme, setTheme];
}
