import { describe, expect, it } from "vitest";

import { normalizeEmail, validateEmail, validatePassword } from "@/lib/validation/auth";

describe("validateEmail", () => {
  it("accepts a valid address, ignoring surrounding whitespace", () => {
    expect(validateEmail("  alice@example.com ")).toBeUndefined();
    expect(normalizeEmail("  alice@example.com ")).toBe("alice@example.com");
  });

  it("requires a value", () => {
    expect(validateEmail("   ")).toBe("Enter your email address.");
  });

  it.each(["alice", "alice@", "@example.com", "alice@example", "a b@example.com", "ålice@example.com"])(
    "rejects %s",
    (email) => {
      expect(validateEmail(email)).toBe("Enter a valid email address, like name@example.com.");
    },
  );

  it("rejects addresses longer than 320 characters", () => {
    expect(validateEmail(`${"a".repeat(310)}@example.com`)).toBeDefined();
  });
});

describe("validatePassword", () => {
  it("requires a value", () => {
    expect(validatePassword("")).toBe("Enter your password.");
  });

  it("rejects 7 characters and accepts 8", () => {
    expect(validatePassword("1234567")).toBe("Password must be at least 8 characters.");
    expect(validatePassword("12345678")).toBeUndefined();
  });

  it("accepts 256 characters and rejects 257", () => {
    expect(validatePassword("a".repeat(256))).toBeUndefined();
    expect(validatePassword("a".repeat(257))).toBe("Password must be at most 256 characters.");
  });

  it("counts characters like the backend, not UTF-16 code units", () => {
    // Four emoji are 8 UTF-16 code units but only 4 characters.
    expect(validatePassword("😀😀😀😀")).toBe("Password must be at least 8 characters.");
  });
});
