import "@fontsource-variable/fraunces";
import "@fontsource-variable/instrument-sans";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { applyStoredTheme } from "./app/theme";
import { loadConfig } from "./config";
import "./styles/tokens.css";
import "./styles/fonts.css";
import "./styles/base.css";
import "./styles/legacy.css";

// Set the theme before React renders, so the page does not flash in the wrong theme.
applyStoredTheme();

const root = createRoot(document.getElementById("root")!);

const CFN_LOOKUP = `aws cloudformation describe-stacks \\
  --stack-name BudgetWebStack-dev \\
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" \\
  --output text`;

loadConfig()
    .then(() => {
        root.render(
            <StrictMode>
                <App />
            </StrictMode>,
        );
    })
    .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        root.render(
            <div className="legacy-page">
                <div className="page">
                    <h1>Couldn't load app config</h1>
                    <p>{message}</p>
                    <p>
                        If you're setting up this repo on a new machine, <code>budget/config.dev.json</code> is missing
                        or invalid. It needs <code>cognitoDomain</code>, <code>clientId</code>, and <code>stage</code>.
                        Pull the current dev <code>clientId</code> from CloudFormation:
                    </p>
                    <pre style={{ whiteSpace: "pre-wrap", background: "#f4f4f4", padding: "0.75rem" }}>
                        {CFN_LOOKUP}
                    </pre>
                </div>
            </div>,
        );
    });
