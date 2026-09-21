import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

const html = readFileSync(resolve("../nanobot/desktop/static/index.html"), "utf8");
const script = readFileSync(resolve("../nanobot/desktop/static/app.js"), "utf8");

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

it("shows all providers before saving or testing and preserves drafts on status refresh", async () => {
  document.body.innerHTML = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i)![1];
  const status = {
    state: "unconfigured", error: "", version: "0.3.5", source: "none",
    configPath: "C:/data/config.json", model: {}, connections: {},
    providers: ["deepseek", "openai", "anthropic", "openrouter", "custom", "dashscope", "gemini", "ollama"],
  };
  const fetchMock = vi.fn(async () => ({ ok: true, json: async () => status }));
  vi.stubGlobal("fetch", fetchMock);
  // happy-dom does not expose the browser's Option constructor.
  vi.stubGlobal("Option", function (text: string, value: string) {
    const option = document.createElement("option");
    option.text = text;
    option.value = value;
    return option;
  });
  vi.spyOn(globalThis, "setInterval").mockImplementation(() => 0 as unknown as ReturnType<typeof setInterval>);
  const app = new Function(`${script}\nreturn {render};`)() as { render: (data: typeof status) => void };
  const provider = document.getElementById("provider") as HTMLSelectElement;
  await waitFor(() => expect(Array.from(provider.options, option => option.value)).toEqual(status.providers));
  expect(fetchMock.mock.calls).toHaveLength(1);
  expect(fetchMock).toHaveBeenCalledWith("/api/status", expect.objectContaining({ method: "GET" }));

  provider.value = "gemini";
  const model = document.getElementById("model-name") as HTMLInputElement;
  const key = document.getElementById("api-key") as HTMLInputElement;
  model.value = "unsaved-model";
  key.value = "unsaved-test-key";
  app.render(status);
  app.render(status);
  expect(provider.value).toBe("gemini");
  expect(model.value).toBe("unsaved-model");
  expect(key.value).toBe("unsaved-test-key");
  expect(provider.options).toHaveLength(status.providers.length);
});
