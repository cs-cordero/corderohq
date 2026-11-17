import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { useAdmin } from "../admin";
import { handleCallback, redirectToLogin } from "../auth";

const CFN_LOOKUP = `aws cloudformation describe-stacks \\
  --stack-name BudgetWebStack-dev \\
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" \\
  --output text`;

export default function CallbackPage() {
    const navigate = useNavigate();
    const { refreshAdminUser } = useAdmin();
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        handleCallback().then((result) => {
            if (result.kind === "success") {
                // The admin provider mounted before the tokens existed, so it reads the groups again.
                refreshAdminUser();
                navigate("/", { replace: true });
            } else if (result.kind === "no-code") {
                redirectToLogin();
            } else {
                setError(result.reason);
            }
        });
    }, [navigate, refreshAdminUser]);

    if (error) {
        return (
            <div className="page">
                <h1>Sign-in failed</h1>
                <p>{error}</p>
                <p>
                    The most likely cause is that <code>clientId</code> in <code>budget/config.dev.json</code> is out of
                    sync with the deployed Cognito app client. Re-fetch it:
                </p>
                <pre style={{ whiteSpace: "pre-wrap", background: "#f4f4f4", padding: "0.75rem" }}>{CFN_LOOKUP}</pre>
            </div>
        );
    }

    return (
        <div className="page">
            <p>Signing in...</p>
        </div>
    );
}
