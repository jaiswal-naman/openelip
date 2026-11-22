import { EmptyState } from "@/components/ui/States";
import { LinkButton } from "@/components/ui/LinkButton";

// Unknown routes and items the person cannot see get the same page, so existence is not revealed (FRONTEND_UI 9.9).
export default function NotFound() {
  return (
    <div className="container page">
      <h1 className="t-page">Not found</h1>
      <EmptyState
        action={
          <LinkButton href="/jobs" variant="primary">
            Search open jobs
          </LinkButton>
        }
      >
        You do not have access to this, or it does not exist.
      </EmptyState>
    </div>
  );
}
