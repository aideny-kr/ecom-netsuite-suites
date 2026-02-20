"use client";

interface CodeViewerProps {
  content: string;
  filePath: string;
}

export function CodeViewer({ content, filePath }: CodeViewerProps) {
  const lines = content.split("\n");

  return (
    <div className="h-full overflow-auto bg-[#1e1e1e] text-[13px] font-mono">
      <div className="p-4">
        {lines.map((line, i) => (
          <div key={i} className="flex">
            <span className="inline-block w-12 shrink-0 select-none pr-4 text-right text-[#858585]">
              {i + 1}
            </span>
            <span className="text-[#d4d4d4] whitespace-pre-wrap break-all">
              {line || "\n"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
