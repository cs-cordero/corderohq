import { useTheme } from "./theme";
import styles from "./AppShell.module.css";

export default function ThemeToggle() {
    const [theme, setTheme] = useTheme();
    const dark = theme === "dark";
    return (
        // The text names the current theme, as in the design. A click changes to the other theme.
        <button type="button" className={styles.themeToggle} onClick={() => setTheme(dark ? "light" : "dark")}>
            <span>{dark ? "Dark mode" : "Light mode"}</span>
            <span aria-hidden="true" className={styles.themeDot} />
        </button>
    );
}
