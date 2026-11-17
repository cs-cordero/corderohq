// Join the class names that are set. A false, null, or undefined value adds nothing.
export function cx(...names: (string | false | null | undefined)[]): string {
    return names.filter(Boolean).join(" ");
}
