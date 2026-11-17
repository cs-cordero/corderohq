import { createBrowserRouter } from "react-router";
import { RouterProvider } from "react-router/dom";
import { AdminProvider } from "./admin";
import { ROUTES } from "./app/routes";

// A data router, because the leave guard needs `useBlocker`.
const router = createBrowserRouter(ROUTES);

export default function App() {
    return (
        <AdminProvider>
            <RouterProvider router={router} />
        </AdminProvider>
    );
}
