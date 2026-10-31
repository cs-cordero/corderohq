import { BrowserRouter, Route, Routes } from "react-router-dom";
import { AdminProvider } from "./admin";
import Layout from "./components/Layout";
import AccountsPage from "./pages/AccountsPage";
import AdminToolsPage from "./pages/AdminToolsPage";
import AuditLogPage from "./pages/AuditLogPage";
import BudgetPage from "./pages/BudgetPage";
import IncomePage from "./pages/IncomePage";
import CallbackPage from "./pages/CallbackPage";
import CategoriesPage from "./pages/CategoriesPage";
import NetWorthEntryPage from "./pages/NetWorthEntryPage";
import SummaryPage from "./pages/SummaryPage";
import TransactionsPage from "./pages/TransactionsPage";
import UploadPage from "./pages/UploadPage";

export default function App() {
    return (
        <AdminProvider>
            <BrowserRouter>
                <Routes>
                    <Route path="/callback" element={<CallbackPage />} />
                    <Route element={<Layout />}>
                        <Route index element={<SummaryPage />} />
                        <Route path="budget" element={<BudgetPage />} />
                        <Route path="income" element={<IncomePage />} />
                        <Route path="transactions" element={<TransactionsPage />} />
                        <Route path="upload" element={<UploadPage />} />
                        <Route path="categories" element={<CategoriesPage />} />
                        <Route path="accounts" element={<AccountsPage />} />
                        <Route path="net-worth" element={<NetWorthEntryPage />} />
                        <Route path="audit-log" element={<AuditLogPage />} />
                        <Route path="admin" element={<AdminToolsPage />} />
                    </Route>
                </Routes>
            </BrowserRouter>
        </AdminProvider>
    );
}
