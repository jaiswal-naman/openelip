import type { components } from "@/lib/api/schema";

/**
 * The job shapes the job screens read, named from the generated API types: when the API changes a field, the type
 * check fails here instead of a screen showing the wrong thing. A `JobDetail` has every field of a `Job`.
 */
export type Job = components["schemas"]["Job"];
export type JobDetail = components["schemas"]["JobDetail"];
export type Requirement = components["schemas"]["Requirement"];
export type Span = components["schemas"]["Span"];
