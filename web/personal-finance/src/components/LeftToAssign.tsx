import { formatCurrency } from "../format";

// The top line of the budget: the planned net income, and the money that has no job yet.
// The goal is $0. A value that is not $0 shows in a different colour. It never blocks a save.
export default function LeftToAssign({ plannedNetIncome, budgeted }: { plannedNetIncome: number; budgeted: number }) {
    const left = plannedNetIncome - budgeted;
    const state = left === 0 ? "balanced" : left > 0 ? "unassigned" : "overassigned";
    return (
        <p className={`left-to-assign left-to-assign-${state}`}>
            Planned net income <strong>{formatCurrency(plannedNetIncome)}</strong> · Left to assign{" "}
            <strong>{formatCurrency(left)}</strong>
            {state === "unassigned" && <span className="muted"> (money with no job yet)</span>}
            {state === "overassigned" && <span className="muted"> (the targets are more than the income)</span>}
        </p>
    );
}
