/**
 * A call that failed, in one shape: a stable `code` (API.md 1.11), the request id for the operator, and how long to
 * wait when the server said so. The message people read comes from `messageFor` in labels.ts, never from here.
 */
export class ApiFailure extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;
  readonly retryAfterSeconds: number | null;

  constructor(init: {
    status: number;
    code: string;
    details?: Record<string, unknown>;
    requestId?: string | null;
    retryAfterSeconds?: number | null;
  }) {
    super(init.code); // the message is the code: nothing a person typed can end up in an error string
    this.status = init.status;
    this.code = init.code;
    this.details = init.details ?? {};
    this.requestId = init.requestId ?? null;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
  }
}

type FetchResult<B> = { data?: B; error?: unknown; response: Response };

/** The code for a response that is not our JSON envelope, for example a bad gateway page from a proxy. */
function codeForStatus(status: number): string {
  if (status === 401) return "unauthenticated";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 429) return "rate_limited";
  if (status === 502 || status === 503 || status === 504) return "dependency_unavailable";
  return status >= 500 ? "internal_error" : "invalid_request";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function positiveInteger(value: unknown): number | null {
  const number = typeof value === "string" ? Number(value) : value;
  return typeof number === "number" && Number.isInteger(number) && number > 0 ? number : null;
}

function failureFrom(error: unknown, response: Response): ApiFailure {
  const body = isRecord(error) ? error : {};
  const detail = isRecord(body.error) ? body.error : null;
  const meta = isRecord(body.meta) ? body.meta : {};
  const details = detail && isRecord(detail.details) ? detail.details : {};
  return new ApiFailure({
    status: response.status,
    code: typeof detail?.code === "string" ? detail.code : codeForStatus(response.status),
    details,
    requestId: typeof meta.request_id === "string" ? meta.request_id : response.headers.get("X-Request-Id"),
    retryAfterSeconds: positiveInteger(response.headers.get("Retry-After")) ?? positiveInteger(details.retry_after_seconds),
  });
}

/** What a typed route answers with: the envelope's `data` (API.md 1.3). An untyped route gives `unknown`. */
type DataOf<B> = B extends { data: infer D } ? NonNullable<D> : unknown;

/** Await an openapi-fetch call and return the envelope's `data`, or throw an ApiFailure. */
export async function unwrap<B>(pending: Promise<FetchResult<B>>): Promise<DataOf<B>> {
  let result: FetchResult<B>;
  try {
    result = await pending;
  } catch {
    throw new ApiFailure({ status: 0, code: "network_error" }); // the browser's own message can name hosts; drop it
  }
  if (result.error !== undefined || !result.response.ok) throw failureFrom(result.error, result.response);
  const body: unknown = result.data;
  if (!isRecord(body) || body.data === null || body.data === undefined) {
    throw new ApiFailure({ status: result.response.status, code: "unreadable_response" });
  }
  return body.data as DataOf<B>;
}

/** Like `unwrap` for a call whose success has no body (204). */
export async function unwrapEmpty(pending: Promise<FetchResult<unknown>>): Promise<void> {
  let result: FetchResult<unknown>;
  try {
    result = await pending;
  } catch {
    throw new ApiFailure({ status: 0, code: "network_error" });
  }
  if (result.error !== undefined || !result.response.ok) throw failureFrom(result.error, result.response);
}

export function isFailure(error: unknown, code?: string): error is ApiFailure {
  return error instanceof ApiFailure && (code === undefined || error.code === code);
}
