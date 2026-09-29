import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { PreparationProgress } from "../preparation-progress";

it("shows what the agent has checked, what is ready and why orders were set aside", () => {
  render(
    <PreparationProgress
      data={{
        checked: 26, total: 34, ready: 20,
        set_aside: [{ label: "waiting on Solidus to finalize", count: 3 }, { label: "period is locked", count: 1 }],
        now: ["R087832289", "R321007055"],
      }}
    />,
  );
  const status = screen.getByRole("status", { name: "Group preparation progress" });
  expect(status).toHaveTextContent("26 of 34 checked");
  expect(status).toHaveTextContent("20 fixes ready");
  expect(status).toHaveTextContent("4 set aside: 3 waiting on Solidus to finalize · 1 period is locked");
  expect(status).toHaveTextContent("Now checking R087832289, R321007055");
});
