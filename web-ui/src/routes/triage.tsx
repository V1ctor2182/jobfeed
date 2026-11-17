import { Navigate, useLocation } from "react-router";

export default function TriagePage() {
  const location = useLocation();
  return <Navigate to={{ pathname: "/runs", search: location.search, hash: location.hash }} replace />;
}
