import type { ChangeScope } from "../types";

// The choice for a change of a budget target or an income payment: from this month on (later
// months carry it forward), or this month only (the next month goes back to the carried value).
export default function ScopeChoice({
    value,
    onChange,
}: {
    value: ChangeScope;
    onChange: (scope: ChangeScope) => void;
}) {
    return (
        <span className="scope-choice">
            <label>
                <input type="radio" checked={value === "FORWARD"} onChange={() => onChange("FORWARD")} /> From this
                month on
            </label>
            <label>
                <input type="radio" checked={value === "THIS_MONTH"} onChange={() => onChange("THIS_MONTH")} /> This
                month only
            </label>
        </span>
    );
}
