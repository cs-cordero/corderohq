import { getConfig } from "./config";

function generateCodeVerifier(): string {
    const array = new Uint8Array(32);
    crypto.getRandomValues(array);
    return btoa(String.fromCharCode(...array))
        .replace(/\+/g, "-")
        .replace(/\//g, "_")
        .replace(/=+$/, "");
}

async function generateCodeChallenge(verifier: string): Promise<string> {
    const encoder = new TextEncoder();
    const data = encoder.encode(verifier);
    const digest = await crypto.subtle.digest("SHA-256", data);
    return btoa(String.fromCharCode(...new Uint8Array(digest)))
        .replace(/\+/g, "-")
        .replace(/\//g, "_")
        .replace(/=+$/, "");
}

export function redirectToLogin(): void {
    const verifier = generateCodeVerifier();
    sessionStorage.setItem("pkce_code_verifier", verifier);

    generateCodeChallenge(verifier).then((challenge) => {
        const params = new URLSearchParams({
            response_type: "code",
            client_id: getConfig().clientId,
            redirect_uri: getConfig().appUrl + getConfig().callbackPath,
            scope: "openid email",
            code_challenge_method: "S256",
            code_challenge: challenge,
        });
        window.location.href = `${getConfig().cognitoDomain}/oauth2/authorize?${params}`;
    });
}

export type CallbackResult = { kind: "success" } | { kind: "no-code" } | { kind: "error"; reason: string };

// Permit one callback at a time. In dev, React StrictMode mounts CallbackPage two times.
// Without this guard, the second call finds that the first call used the PKCE verifier
// already. It then sends the user to the sign-in page, and the OAuth loop never ends.
let _callbackPromise: Promise<CallbackResult> | null = null;

export function handleCallback(): Promise<CallbackResult> {
    if (_callbackPromise) return _callbackPromise;

    _callbackPromise = (async () => {
        const params = new URLSearchParams(window.location.search);

        const cognitoError = params.get("error");
        if (cognitoError) {
            const desc = params.get("error_description");
            return { kind: "error", reason: desc ? `${cognitoError}: ${desc}` : cognitoError };
        }

        const code = params.get("code");
        const verifier = sessionStorage.getItem("pkce_code_verifier");

        if (!code || !verifier) return { kind: "no-code" };

        sessionStorage.removeItem("pkce_code_verifier");

        const response = await fetch("/oauth2/token", {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams({
                grant_type: "authorization_code",
                client_id: getConfig().clientId,
                redirect_uri: getConfig().appUrl + getConfig().callbackPath,
                code,
                code_verifier: verifier,
            }),
        });

        if (!response.ok) {
            const body = await response.text().catch(() => "");
            return { kind: "error", reason: `Token exchange failed: ${response.status} ${body}`.trim() };
        }

        const tokens = await response.json();
        storeTokens(tokens);
        return { kind: "success" };
    })();

    return _callbackPromise;
}

function storeTokens(tokens: { access_token: string; id_token: string; refresh_token?: string; expires_in: number }) {
    sessionStorage.setItem("access_token", tokens.access_token);
    sessionStorage.setItem("id_token", tokens.id_token);
    if (tokens.refresh_token) {
        sessionStorage.setItem("refresh_token", tokens.refresh_token);
    }
    const expiresAt = Date.now() + tokens.expires_in * 1000;
    sessionStorage.setItem("token_expires_at", expiresAt.toString());
}

// Deduplicates concurrent refresh attempts — multiple parallel API calls that
// all find an expired token will share a single in-flight refresh request
// instead of racing against each other.
let _refreshPromise: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
    if (_refreshPromise) return _refreshPromise;

    _refreshPromise = (async () => {
        const refreshToken = sessionStorage.getItem("refresh_token");
        if (!refreshToken) return false;

        const response = await fetch("/oauth2/token", {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams({
                grant_type: "refresh_token",
                client_id: getConfig().clientId,
                refresh_token: refreshToken,
            }),
        });

        if (!response.ok) return false;

        const tokens = await response.json();
        storeTokens(tokens);
        return true;
    })();

    try {
        return await _refreshPromise;
    } finally {
        _refreshPromise = null;
    }
}

export async function getAccessToken(): Promise<string> {
    const expiresAt = parseInt(sessionStorage.getItem("token_expires_at") ?? "0", 10);
    const token = sessionStorage.getItem("access_token");

    if (token && Date.now() < expiresAt - 60_000) {
        return token;
    }

    try {
        const refreshed = await refreshAccessToken();
        if (refreshed) {
            return sessionStorage.getItem("access_token")!;
        }
    } catch {
        // Refresh failed — fall through to login redirect
    }

    redirectToLogin();
    return new Promise(() => {}); // never resolves — page navigates away
}

export function isAuthenticated(): boolean {
    const expiresAt = parseInt(sessionStorage.getItem("token_expires_at") ?? "0", 10);
    return !!sessionStorage.getItem("access_token") && Date.now() < expiresAt;
}

export function signOut(): void {
    sessionStorage.clear();
    const params = new URLSearchParams({
        client_id: getConfig().clientId,
        logout_uri: getConfig().appUrl + getConfig().logoutPath,
    });
    window.location.href = `${getConfig().cognitoDomain}/logout?${params}`;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function base64urlDecode(input: string): string {
    const normalized = input.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
    const binary = atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    return new TextDecoder("utf-8").decode(bytes);
}

function decodeJwtPayload(token: string): unknown {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    try {
        return JSON.parse(base64urlDecode(parts[1]));
    } catch {
        return null;
    }
}

/**
 * Return the email of the signed-in user, from the `email` claim of the ID token, else its
 * `cognito:username` claim. The access token holds no email, so this reads the ID token. Returns
 * null if the user is not signed in or the token holds neither claim.
 */
export function getCurrentUserEmail(): string | null {
    const token = sessionStorage.getItem("id_token");
    if (!token) return null;
    const payload = decodeJwtPayload(token);
    if (!isPlainObject(payload)) return null;
    for (const claim of ["email", "cognito:username"]) {
        const value = payload[claim];
        if (typeof value === "string" && value) return value;
    }
    return null;
}

/**
 * Return the user's Cognito group memberships, parsed from the access token's
 * `cognito:groups` claim. The claim can arrive as either a JSON-encoded list or
 * a comma-separated string depending on API Gateway settings; both shapes are
 * handled. Returns `[]` if not signed in or the claim is absent.
 */
export function getCurrentUserGroups(): string[] {
    const token = sessionStorage.getItem("access_token");
    if (!token) return [];
    const payload = decodeJwtPayload(token);
    if (!isPlainObject(payload)) return [];
    const groups = payload["cognito:groups"];
    if (Array.isArray(groups)) {
        return groups.filter((g): g is string => typeof g === "string");
    }
    if (typeof groups === "string") {
        return groups
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
    }
    return [];
}
