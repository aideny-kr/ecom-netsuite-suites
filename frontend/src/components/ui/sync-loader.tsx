"use client";

import { motion } from "framer-motion";

export function SyncLoader() {
    return (
        <div className="flex flex-col items-center justify-center p-6 bg-background rounded-lg border border-border/50 shadow-sm relative overflow-hidden">
            {/* Subtle background glow */}
            <div className="absolute inset-0 bg-blue-500/5 blur-[40px] pointer-events-none" />

            <div className="relative flex items-center justify-center h-20 w-20 mb-4">
                {/* Outer spinning dashed ring */}
                <motion.div
                    className="absolute inset-0 rounded-full border-[3px] border-dashed border-blue-200"
                    animate={{ rotate: 360 }}
                    transition={{ duration: 8, repeat: Infinity, ease: "linear" }}
                />
                {/* Inner fast spinning multi-colored ring */}
                <motion.div
                    className="absolute inset-2 rounded-full border-[3px] border-t-blue-600 border-r-indigo-500 border-b-sky-400 border-l-transparent"
                    animate={{ rotate: -360 }}
                    transition={{ duration: 1.5, repeat: Infinity, ease: "linear" }}
                />
                {/* Pulsing center dot */}
                <motion.div
                    className="w-4 h-4 rounded-full bg-blue-600"
                    animate={{ scale: [1, 1.3, 1], opacity: [0.7, 1, 0.7] }}
                    transition={{ duration: 1.5, repeat: Infinity, ease: "easeInOut" }}
                />
            </div>

            <motion.div
                className="flex flex-col items-center"
                initial={{ opacity: 0, y: 5 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.1 }}
            >
                <h3 className="text-[15px] font-semibold text-foreground mb-1 bg-clip-text text-transparent bg-gradient-to-r from-blue-700 to-indigo-600">
                    Syncing NetSuite Environment
                </h3>
                <div className="flex items-center gap-1.5 text-[13px] text-muted-foreground">
                    <span>Extracting folders and SuiteScripts</span>
                    <motion.span
                        animate={{ opacity: [0, 1, 0] }}
                        transition={{ duration: 1.5, repeat: Infinity, ease: "linear" }}
                    >
                        ...
                    </motion.span>
                </div>
            </motion.div>
        </div>
    );
}
