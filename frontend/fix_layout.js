const fs = require('fs');
let content = fs.readFileSync('src/app/(dashboard)/workspace/page.tsx', 'utf8');

// Replace the nested right group to un-nest them, and apply absolute min-w-0 to all panels
content = content.replace(/<Panel id="workspace-right-wrapper".*?<\/PanelResizeHandle>/s, '');
content = content.replace(/<\/PanelGroup>\s*<\/Panel>\s*<\/PanelGroup>/s, '</PanelGroup>');
content = content.replace(/<PanelGroup orientation="horizontal" className="flex h-full flex-col overflow-hidden">/g, '');
content = content.replace(/<Panel id="workspace-mid" defaultSize={65} minSize={30} style={{ minWidth: 0 }}>/, '<Panel id="workspace-mid" defaultSize={55} minSize={30} className="min-w-0">');
content = content.replace(/<Panel id="workspace-right" defaultSize={35} minSize={25} maxSize={50} collapsible collapsedSize={0} style={{ minWidth: 0 }}>/, '<PanelResizeHandle className="group relative hover:bg-primary/10 active:bg-primary/20 transition-colors" style={{ flexBasis: "8px" }}><div className="pointer-events-none absolute inset-y-0 left-1/2 -translate-x-1/2 w-px bg-border group-hover:bg-primary/50 transition-colors" /></PanelResizeHandle>\n          <Panel id="workspace-right" defaultSize={25} minSize={20} maxSize={50} collapsible collapsedSize={0} className="min-w-0">');
content = content.replace(/<Panel\s*id="workspace-left"\s*style={{ minWidth: 0 }}/, '<Panel id="workspace-left" defaultSize={20} minSize={15} className="min-w-0"');

fs.writeFileSync('src/app/(dashboard)/workspace/page.tsx', content);
console.log("Replaced layout");
