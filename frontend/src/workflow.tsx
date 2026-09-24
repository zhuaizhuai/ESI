import { useEffect, useState } from "react";
import { api } from "./session";

type Step = { key: string; name: string; position: number; status: string };

export function WorkflowStrip({
  kind,
  jobId,
  status,
}: {
  kind: "development" | "analysis";
  jobId: string;
  status: string;
}) {
  const [steps, setSteps] = useState<Step[]>([]);
  useEffect(() => {
    api(`/platform/workflows/${kind}/${jobId}`)
      .then(setSteps)
      .catch(() => setSteps([]));
  }, [kind, jobId, status]);
  if (!steps.length) return null;
  return (
    <section className="panel">
      <h2>任务步骤</h2>
      <ol className="platform-steps">
        {steps.map((step) => (
          <li className={step.status} key={step.key}>
            <span>{step.position + 1}</span>
            {step.name}
            <small>{step.status === "done" ? "已完成" : step.status === "active" ? "进行中" : step.status === "pending" ? "待开始" : step.status}</small>
          </li>
        ))}
      </ol>
    </section>
  );
}
