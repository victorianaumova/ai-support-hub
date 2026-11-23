import { describe, expect, it } from "vitest";
import { ApiError, apiError, describe as describeError } from "./problem";

describe("apiError", () => {
  it("keeps the code, request id and Retry-After of a problem answer", () => {
    const response = new Response(null, { status: 429, headers: { "X-Request-ID": "abc", "Retry-After": "30" } });
    const error = apiError(response, { status: 429, code: "too_many_requests", title: "Too Many Requests", detail: "slow down" });

    expect(error.code).toBe("too_many_requests");
    expect(error.requestId).toBe("abc");
    expect(error.retryAfterS).toBe(30);
    expect(describeError(error)).toContain("Too many requests");
  });

  it("names an answer without problem details by its status", () => {
    const error = apiError(new Response(null, { status: 502 }), "<html>");
    expect(error.code).toBe("http_502");
    expect(error.problem).toBeNull();
  });

  it("describes a network failure without internals", () => {
    expect(describeError(new TypeError("Failed to fetch"))).toBe("The connection failed. Check your network and try again.");
    expect(describeError(new ApiError(422, { status: 422, code: "validation_error", title: "", detail: "Bad input." }, null, null))).toBe("Bad input.");
  });
});
