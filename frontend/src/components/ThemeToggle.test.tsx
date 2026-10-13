import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ThemeToggle } from "./ThemeToggle";

describe("ThemeToggle", () => {
  it("follows the system by default", () => {
    render(<ThemeToggle />);
    expect(screen.getByRole("button", { name: "Wie System" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(document.documentElement).not.toHaveClass("dark");
  });

  it("switches to dark and back to light", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    await user.click(screen.getByRole("button", { name: "Dunkel" }));
    expect(document.documentElement).toHaveClass("dark");
    expect(localStorage.getItem("twinraid.theme")).toBe("dark");

    await user.click(screen.getByRole("button", { name: "Hell" }));
    expect(document.documentElement).not.toHaveClass("dark");
    expect(localStorage.getItem("twinraid.theme")).toBe("light");
  });

  it("restores the stored choice", () => {
    localStorage.setItem("twinraid.theme", "dark");
    render(<ThemeToggle />);
    expect(screen.getByRole("button", { name: "Dunkel" })).toHaveAttribute("aria-pressed", "true");
    expect(document.documentElement).toHaveClass("dark");
  });
});
