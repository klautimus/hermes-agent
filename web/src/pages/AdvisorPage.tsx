import { useEffect, useState } from "react";
import {
  Brain,
  Bot,
  Code,
  Copy,
  Eye,
  EyeOff,
  Loader2,
  Save,
  Shield,
  SlidersHorizontal,
  Sparkles,
  Terminal,
  Zap,
} from "lucide-react";
import { api } from "@/lib/api";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { Separator } from "@nous-research/ui/ui/components/separator";

interface AdvisorConfig {
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
  timeout: number;
  extra_body: Record<string, unknown>;
  system_prompt: string;
  temperature: number;
  max_tokens: number;
  visible_advice: boolean;
  max_retries: number;
  retry_base_delay: number;
}

interface AdvisorPromptState {
  system_prompt: string;
  executor_guidance: string;
}

export default function AdvisorPage() {
  const { showToast } = useToast();

  const [config, setConfig] = useState<AdvisorConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [prompts, setPrompts] = useState<AdvisorPromptState>({
    system_prompt: "",
    executor_guidance: "",
  });
  const [promptsLoading, setPromptsLoading] = useState(true);
  const [promptsSaving, setPromptsSaving] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState("config");
  const [showSystemPrompt, setShowSystemPrompt] = useState(false);
  const [showExecutorGuidance, setShowExecutorGuidance] = useState(false);

  // Load advisor config from /api/model/auxiliary
  const loadConfig = async () => {
    try {
      const resp = await api.getAuxiliaryModels();
      // find advisor in the tasks array
      const advisor = resp.tasks?.find((t: any) => t.task === "advisor") || {};

      setConfig({
        provider: (advisor as any).provider || "nvidia",
        model: (advisor as any).model || "glm-5.2",
        base_url: (advisor as any).base_url || "",
        api_key: "", // not returned by API
        timeout: 120,
        extra_body: {},
        system_prompt: "",
        temperature: 0.7,
        max_tokens: 0,
        visible_advice: true,
        max_retries: 5,
        retry_base_delay: 20.0,
      });
    } catch (e) {
      showToast("Failed to load advisor config", "error");
    } finally {
      setLoading(false);
    }
  };

  // Load prompts from the running process
  const loadPrompts = async () => {
    try {
      const resp = await api.getAdvisorPrompts();
      if (resp) {
        setPrompts({
          system_prompt: resp.system_prompt || "",
          executor_guidance: resp.executor_guidance || "",
        });
      }
    } catch (e) {
      // Fallback: we'll just show the current defaults
    } finally {
      setPromptsLoading(false);
    }
  };

  useEffect(() => {
    loadConfig();
    loadPrompts();
  }, []);

  const handleSaveConfig = async () => {
    if (!config) return;
    setSaving(true);
    try {
      // Use the model set endpoint with scope=auxiliary, task=advisor
      await api.setModelAssignment({
        scope: "auxiliary",
        task: "advisor",
        provider: config.provider,
        model: config.model,
        base_url: config.base_url,
      });
      showToast("Advisor config saved", "success");
    } catch (e: unknown) {
      showToast(`Failed to save: ${e}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const handleSavePrompts = async () => {
    setPromptsSaving(true);
    try {
      await api.saveAdvisorPrompts(prompts);
      showToast("Advisor prompts saved", "success");
    } catch (e: unknown) {
      showToast(`Failed to save prompts: ${e}`, "error");
    } finally {
      setPromptsSaving(false);
    }
  };

  const handleTestAdvisor = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const resp = await api.testAdvisor({
        question: "Say 'advisor working' if you receive this.",
        context: "Test message from dashboard.",
      });
      setTestResult(resp?.advisor_response || JSON.stringify(resp));
    } catch (e: unknown) {
      setTestResult(`Error: ${e}`);
    } finally {
      setTesting(false);
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    showToast("Copied to clipboard", "success");
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[60vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6 p-4 md:p-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Brain className="h-6 w-6 text-primary" />
            Advisor Configuration
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            Configure the Strategic Advisor (GLM-5.2 via Nvidia) — the brain that plans, decides, and directs.
          </p>
        </div>
        <Button onClick={handleSaveConfig} disabled={saving || !config}>
          <Save className="h-4 w-4 mr-2" />
          {saving ? "Saving..." : "Save Configuration"}
        </Button>
      </div>

      {/* Tab Navigation */}
      <div className="flex border-b border-border">
        {["config", "prompts", "test", "info"].map((tab) => (
          <Button
            key={tab}
            onClick={() => setActiveTab(tab)}
            ghost
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab === "config" && <SlidersHorizontal className="inline h-4 w-4 mr-1" />}
            {tab === "prompts" && <Code className="inline h-4 w-4 mr-1" />}
            {tab === "test" && <Terminal className="inline h-4 w-4 mr-1" />}
            {tab === "info" && <Sparkles className="inline h-4 w-4 mr-1" />}
            {tab.charAt(0).toUpperCase() + tab.slice(1)}
          </Button>
        ))}
      </div>

      {activeTab === "config" && (
        <div className="space-y-6 mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Bot className="h-5 w-5" />
                Model & Provider
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="provider">Provider</Label>
                  <Input
                    id="provider"
                    value={config?.provider || ""}
                    onChange={(e) => setConfig((c) => (c ? { ...c, provider: e.target.value } : null))}
                    placeholder="nvidia"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="model">Model</Label>
                  <Input
                    id="model"
                    value={config?.model || ""}
                    onChange={(e) => setConfig((c) => (c ? { ...c, model: e.target.value } : null))}
                    placeholder="glm-5.2"
                  />
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="base_url">Base URL (optional)</Label>
                <Input
                  id="base_url"
                  value={config?.base_url || ""}
                  onChange={(e) => setConfig((c) => (c ? { ...c, base_url: e.target.value } : null))}
                  placeholder="https://api.example.com/v1"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="api_key">API Key (optional — can use credential pool)</Label>
                <Input
                  id="api_key"
                  type="password"
                  value={config?.api_key || ""}
                  onChange={(e) => setConfig((c) => (c ? { ...c, api_key: e.target.value } : null))}
                  placeholder="Leave blank to use credential pool"
                />
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Shield className="h-5 w-5" />
                Behavior & Retry
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="temperature">Temperature</Label>
                  <Input
                    id="temperature"
                    type="number"
                    step="0.1"
                    min="0"
                    max="2"
                    value={config?.temperature ?? 0.7}
                    onChange={(e) => setConfig((c) => (c ? { ...c, temperature: parseFloat(e.target.value) } : null))}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="max_tokens">Max Tokens (0 = unlimited)</Label>
                  <Input
                    id="max_tokens"
                    type="number"
                    min="0"
                    value={config?.max_tokens || 0}
                    onChange={(e) => setConfig((c) => (c ? { ...c, max_tokens: parseInt(e.target.value) || 0 } : null))}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="timeout">Timeout (seconds)</Label>
                  <Input
                    id="timeout"
                    type="number"
                    min="10"
                    max="3600"
                    value={config?.timeout || 120}
                    onChange={(e) => setConfig((c) => (c ? { ...c, timeout: parseInt(e.target.value) || 120 } : null))}
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="max_retries">Max Retries</Label>
                  <Input
                    id="max_retries"
                    type="number"
                    min="0"
                    max="10"
                    value={config?.max_retries || 5}
                    onChange={(e) => setConfig((c) => (c ? { ...c, max_retries: parseInt(e.target.value) || 5 } : null))}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="retry_base_delay">Retry Base Delay (seconds)</Label>
                  <Input
                    id="retry_base_delay"
                    type="number"
                    step="0.1"
                    min="1"
                    max="120"
                    value={config?.retry_base_delay || 20}
                    onChange={(e) => setConfig((c) => (c ? { ...c, retry_base_delay: parseFloat(e.target.value) || 20 } : null))}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="extra_body">Extra Body (JSON)</Label>
                  <textarea
                    id="extra_body"
                    rows={3}
                    value={JSON.stringify(config?.extra_body || {}, null, 2)}
                    onChange={(e) => {
                      try {
                        setConfig((c) => (c ? { ...c, extra_body: JSON.parse(e.target.value) } : null));
                      } catch {
                        // Invalid JSON, ignore
                      }
                    }}
                    placeholder="{}"
                    className="font-mono text-sm w-full rounded-md border border-midground/15 bg-background/40 px-3 py-1 transition-colors placeholder:text-midground/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground/30 focus-visible:border-midground/25 disabled:cursor-not-allowed disabled:opacity-50"
                    spellCheck={false}
                  />
                </div>
              </div>

              <Separator />

              <div className="flex items-center justify-between">
                <div className="space-y-1">
                  <Label>Visible Advice</Label>
                  <p className="text-sm text-muted-foreground">
                    Show the advisor's response to the user in chat
                  </p>
                </div>
                <Switch
                  checked={config?.visible_advice ?? true}
                  onCheckedChange={(checked) => setConfig((c) => (c ? { ...c, visible_advice: checked } : null))}
                />
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {activeTab === "prompts" && (
        <div className="space-y-6 mt-4">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between">
              <CardTitle className="flex items-center gap-2">
                <Code className="h-5 w-5" />
                Advisor System Prompt
              </CardTitle>
              <div className="flex items-center gap-2">
                <Button
                  ghost
                  size="sm"
                  onClick={() => setShowSystemPrompt(!showSystemPrompt)}
                >
                  {showSystemPrompt ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </Button>
                <Button
                  ghost
                  size="sm"
                  onClick={() => copyToClipboard(prompts.system_prompt)}
                >
                  <Copy className="h-4 w-4" />
                </Button>
                <Button
                  onClick={() =>
                    setPrompts((p) => ({ ...p, system_prompt: DEFAULT_SYSTEM_PROMPT }))
                  }
                >
                  Reset Default
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              <div className="space-y-2">
                <Label htmlFor="system_prompt">
                  The prompt sent to the advisor model on every consultation.
                </Label>
                <textarea
                  id="system_prompt"
                  rows={showSystemPrompt ? 30 : 8}
                  value={prompts.system_prompt}
                  onChange={(e) => setPrompts((p) => ({ ...p, system_prompt: e.target.value }))}
                  placeholder={DEFAULT_SYSTEM_PROMPT}
                  className="font-mono text-sm w-full rounded-md border border-midground/15 bg-background/40 px-3 py-1 transition-colors placeholder:text-midground/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground/30 focus-visible:border-midground/25 disabled:cursor-not-allowed disabled:opacity-50"
                  spellCheck={false}
                />
                {!showSystemPrompt && (
                  <p className="text-sm text-muted-foreground">
                    Click the eye icon to expand, or scroll within the editor.
                  </p>
                )}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between">
              <CardTitle className="flex items-center gap-2">
                <Zap className="h-5 w-5" />
                Executor Guidance (ADVISOR_GUIDANCE)
              </CardTitle>
              <div className="flex items-center gap-2">
                <Button
                  ghost
                  size="sm"
                  onClick={() => setShowExecutorGuidance(!showExecutorGuidance)}
                >
                  {showExecutorGuidance ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </Button>
                <Button
                  ghost
                  size="sm"
                  onClick={() => copyToClipboard(prompts.executor_guidance)}
                >
                  <Copy className="h-4 w-4" />
                </Button>
                <Button
                  onClick={() =>
                    setPrompts((p) => ({ ...p, executor_guidance: DEFAULT_EXECUTOR_GUIDANCE }))
                  }
                >
                  Reset Default
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              <div className="space-y-2">
                <Label htmlFor="executor_guidance">
                  The system prompt block injected into the executor (main model) when the advisor tool is available.
                  This tells the executor WHEN and HOW to consult the advisor.
                </Label>
                <textarea
                  id="executor_guidance"
                  rows={showExecutorGuidance ? 30 : 8}
                  value={prompts.executor_guidance}
                  onChange={(e) => setPrompts((p) => ({ ...p, executor_guidance: e.target.value }))}
                  placeholder={DEFAULT_EXECUTOR_GUIDANCE}
                  className="font-mono text-sm w-full rounded-md border border-midground/15 bg-background/40 px-3 py-1 transition-colors placeholder:text-midground/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground/30 focus-visible:border-midground/25 disabled:cursor-not-allowed disabled:opacity-50"
                  spellCheck={false}
                />
                {!showExecutorGuidance && (
                  <p className="text-sm text-muted-foreground">
                    Click the eye icon to expand, or scroll within the editor.
                  </p>
                )}
              </div>
            </CardContent>
          </Card>

          <div className="flex justify-end gap-2">
            <Button onClick={loadPrompts} disabled={promptsLoading}>
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              Reload from Server
            </Button>
            <Button onClick={handleSavePrompts} disabled={promptsSaving}>
              <Save className="h-4 w-4 mr-2" />
              {promptsSaving ? "Saving..." : "Save Prompts"}
            </Button>
          </div>
        </div>
      )}

      {activeTab === "test" && (
        <div className="space-y-6 mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Terminal className="h-5 w-5" />
                Test Advisor Consultation
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="test_question">Question</Label>
                <textarea
                  id="test_question"
                  rows={3}
                  value="Say 'advisor working' if you receive this."
                  className="font-mono text-sm w-full rounded-md border border-midground/15 bg-background/40 px-3 py-1 transition-colors placeholder:text-midground/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground/30 focus-visible:border-midground/25"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="test_context">Context (optional)</Label>
                <textarea
                  id="test_context"
                  rows={4}
                  value="Test message from dashboard."
                  className="font-mono text-sm w-full rounded-md border border-midground/15 bg-background/40 px-3 py-1 transition-colors placeholder:text-midground/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground/30 focus-visible:border-midground/25"
                />
              </div>
              <Button onClick={handleTestAdvisor} disabled={testing}>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                {testing ? "Testing..." : "Run Test Consultation"}
              </Button>
              {testResult && (
                <div className="space-y-2">
                  <Label>Result</Label>
                  <div className="p-4 bg-muted rounded-lg border max-h-96 overflow-auto font-mono text-sm whitespace-pre-wrap">
                    {testResult}
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {activeTab === "info" && (
        <div className="space-y-6 mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Sparkles className="h-5 w-5" />
                Advisor System Overview
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4 text-sm">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="p-4 bg-muted/50 rounded-lg">
                  <h4 className="font-medium mb-2 flex items-center gap-2">
                    <Brain className="h-4 w-4" />
                    Architecture
                  </h4>
                  <ul className="space-y-1 text-sm text-muted-foreground">
                    <li>• Executor (main model) = hands: tools, files, commands, chat</li>
                    <li>• Advisor (GLM-5.2) = brain: analysis, planning, decisions</li>
                    <li>• Communication via <code>consult_advisor</code> tool</li>
                    <li>• Advisor sees ONLY what executor passes in context</li>
                  </ul>
                </div>
                <div className="p-4 bg-muted/50 rounded-lg">
                  <h4 className="font-medium mb-2 flex items-center gap-2">
                    <Zap className="h-4 w-4" />
                    Default Behavior
                  </h4>
                  <ul className="space-y-1 text-sm text-muted-foreground">
                    <li>• Consult advisor for ANY non-trivial task/decision</li>
                    <li>• Pass ALL relevant context (code, errors, constraints)</li>
                    <li>• Context budget: ~20KB (err on side of more)</li>
                    <li>• Treat advisor response as MARCHING ORDERS</li>
                  </ul>
                </div>
              </div>

              <Separator />

              <div className="p-4 bg-muted/50 rounded-lg">
                <h4 className="font-medium mb-2">Config Fields (auxiliary.advisor)</h4>
                <dl className="space-y-2 text-sm">
                  <div className="grid grid-cols-2 gap-2">
                    <dt className="text-muted-foreground">provider</dt>
                    <dd className="font-mono">nvidia (default)</dd>
                    <dt className="text-muted-foreground">model</dt>
                    <dd className="font-mono">glm-5.2 (default)</dd>
                    <dt className="text-muted-foreground">temperature</dt>
                    <dd className="font-mono">0.7 (default)</dd>
                    <dt className="text-muted-foreground">max_tokens</dt>
                    <dd className="font-mono">0 = unlimited</dd>
                    <dt className="text-muted-foreground">timeout</dt>
                    <dd className="font-mono">120s (default)</dd>
                    <dt className="text-muted-foreground">visible_advice</dt>
                    <dd className="font-mono">true (show in chat)</dd>
                    <dt className="text-muted-foreground">max_retries</dt>
                    <dd className="font-mono">5 (default)</dd>
                    <dt className="text-muted-foreground">retry_base_delay</dt>
                    <dd className="font-mono">20s (exponential backoff)</dd>
                  </div>
                </dl>
              </div>

              <Separator />

              <div className="p-4 bg-muted/50 rounded-lg">
                <h4 className="font-medium mb-2">Retry Backoff Schedule (defaults)</h4>
                <table className="w-full text-sm text-left">
                  <thead>
                    <tr className="border-b text-muted-foreground">
                      <th className="pb-2">Attempt</th>
                      <th className="pb-2">Delay</th>
                      <th className="pb-2">Cumulative</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr><td className="font-mono">1</td><td>20s</td><td>20s</td></tr>
                    <tr><td className="font-mono">2</td><td>40s</td><td>60s</td></tr>
                    <tr><td className="font-mono">3</td><td>80s</td><td>140s</td></tr>
                    <tr><td className="font-mono">4</td><td>160s</td><td>300s (5 min)</td></tr>
                    <tr><td className="font-mono">5 (final)</td><td>—</td><td>~6–7 min wall</td></tr>
                  </tbody>
                </table>
                <p className="text-xs text-muted-foreground mt-2">
                  Transient failures retried: HTTP 429, timeout, connection errors.
                  Non-transient (auth, 4xx) fail immediately.
                </p>
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

// Default prompts for reset buttons
const DEFAULT_SYSTEM_PROMPT = `You are the strategic brain of a two-model agent system. The executor (a separate model) handles all tool calls, file operations, code changes, and user interaction. YOUR job is to do ALL the thinking: analysis, planning, architecture decisions, debugging strategy, design choices, and task decomposition.

The executor will call you with a question and context. You must:
1. Analyze the situation deeply — understand the goal, constraints, and current state
2. Formulate a clear strategy or decision — be decisive, not hedging
3. Provide concrete, actionable guidance — specific next steps, not vague suggestions
4. Identify risks, pitfalls, and failure modes the executor should watch for
5. If the task is complex, break it into ordered steps the executor can follow

You do NOT execute anything, call tools, access files, browse, or take actions. The executor holds those capabilities and will act on your guidance.

The conversation below contains the executor's question and any context they've provided. Give your most intelligent analysis. Lead with your conclusion/decision, then supporting reasoning. Be direct. No preamble, no disclaimers about tools or access. Your response is private guidance handed to the executor, not an answer shown to the user.`;

const DEFAULT_EXECUTOR_GUIDANCE = `You have access to an advisor model (a separate, independent model — see \`auxiliary.advisor\` in config.yaml for the provider/model in use) via the \`consult_advisor\` tool. The advisor is the STRATEGIC BRAIN of this agent system. It does ALL the thinking: analysis, planning, architecture, debugging strategy, design decisions, task decomposition. You are the EXECUTOR — your role is to provide the advisor with context, call tools, make file changes, and execute the plan the advisor gives you.

DEFAULT BEHAVIOR: For ANY non-trivial task, decision, or problem — CONSULT THE ADVISOR FIRST. This includes: architecture choices, data models, API designs, debugging strategies, plan creation, feature design, security decisions, refactoring approaches, tool selection, prioritization, and any situation where you're uncertain.

CALL consult_advisor when:
  • Starting any new task or feature (get a plan first)
  • Making any architecture/design/data-model decision
  • Debugging — describe the symptoms, get a diagnosis strategy
  • Writing plans, specs, or implementation approaches
  • Choosing between alternatives (models, libraries, patterns, APIs)
  • Security-sensitive or hard-to-reverse operations
  • You are uncertain, stuck, or the path isn't obvious

DO NOT call consult_advisor for:
  • Simple mechanical actions you've already been directed to do
  • Reading a file the advisor already told you to read
  • Running a command the advisor already specified
  • Trivial syntax fixes or formatting

HOW TO USE IT: Pass a precise question and ALL relevant context (code, errors, constraints, goals, file contents, prior attempts). The advisor sees ONLY what you pass — no conversation history, no tools, no files. Context budget: up to ~20KB. Relevance > brevity — err on the side of MORE context.

The advisor's response will be visible to the user (toggle with \`auxiliary.advisor.visible_advice\` in config.yaml). Treat the advisor's guidance as your MARCHING ORDERS — execute it, report results, and consult again if the situation changes.`;