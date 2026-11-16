import "@testing-library/jest-dom/vitest";

// Rendered trees are unmounted between tests by React Testing Library's
// automatic cleanup, which it registers with the global `afterEach` (enabled
// by `globals: true` in vitest.config.mts) when a test file imports it.
// Do not import "@testing-library/react" here: it would then register that hook
// from the setup file, which fails under Vitest 5 ("failed to find the current
// suite").
