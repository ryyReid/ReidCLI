from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    name: str
    description: str
    kind: str
    base_url: str
    default_model: str
    aliases: list[str] = field(default_factory=list)
    auth_method: str = "bearer"
    extra_headers: dict[str, str] = field(default_factory=dict)
    popular: bool = False
    icon: str = ""


_BUILTIN: list[ProviderDefinition] = [
    ProviderDefinition(
        id="openai", name="OpenAI", description="GPT-4o, o-series, and DALL-E models",
        kind="openai", base_url="https://api.openai.com", default_model="gpt-4o-mini",
        aliases=["gpt", "chatgpt"], popular=True, icon="🟢",
    ),
    ProviderDefinition(
        id="anthropic", name="Anthropic", description="Claude Sonnet, Opus, and Haiku models",
        kind="anthropic", base_url="https://api.anthropic.com", default_model="claude-sonnet-4-20250514",
        aliases=["claude"], auth_method="x-api-key", popular=True, icon="🟠",
    ),
    ProviderDefinition(
        id="google-ai", name="Google AI", description="Gemini Pro, Flash, and Flash-Lite models",
        kind="openai-compatible", base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="gemini-2.0-flash", aliases=["gemini", "google", "google-gemini"],
        popular=True, icon="🔵",
    ),
    ProviderDefinition(
        id="openrouter", name="OpenRouter", description="Unified API for 300+ models from all providers",
        kind="openai-compatible", base_url="https://openrouter.ai/api/v1",
        default_model="auto", aliases=["or"], popular=True, icon="🔀",
    ),
    ProviderDefinition(
        id="zai", name="Z.ai", description="GLM-4 and GLM-4.5 series models",
        kind="openai-compatible", base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4", aliases=["z-ai", "zhipu", "glm", "bigmodel"], popular=True, icon="🟣",
    ),
    ProviderDefinition(
        id="agentrouter", name="AgentRouter", description="Multi-model routing for AI agents",
        kind="openai-compatible", base_url="https://api.agentrouter.io/v1",
        default_model="auto", aliases=["agent-router"], icon="🤖",
    ),
    ProviderDefinition(
        id="together", name="Together AI", description="Open-source Llama, Mixtral, and Qwen models",
        kind="openai-compatible", base_url="https://api.together.xyz/v1",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo", aliases=["together-ai"],
        popular=True, icon="🤝",
    ),
    ProviderDefinition(
        id="fireworks", name="Fireworks AI", description="Fast inference for open-source models",
        kind="openai-compatible", base_url="https://api.fireworks.ai/inference/v1",
        default_model="accounts/fireworks/models/llama-v3p1-70b-instruct",
        aliases=["fireworks-ai"], popular=True, icon="🎆",
    ),
    ProviderDefinition(
        id="groq", name="Groq", description="Ultra-fast LPU inference for Llama and Mixtral",
        kind="openai-compatible", base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile", aliases=[], popular=True, icon="⚡",
    ),
    ProviderDefinition(
        id="deepinfra", name="DeepInfra", description="Cost-effective inference for open models",
        kind="openai-compatible", base_url="https://api.deepinfra.com/v1/openai",
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct", aliases=[], icon="🔧",
    ),
    ProviderDefinition(
        id="cerebras", name="Cerebras", description="Fastest AI inference with Wafer-Scale Engines",
        kind="openai-compatible", base_url="https://api.cerebras.ai/v1",
        default_model="llama3.1-70b", aliases=[], icon="🧠",
    ),
    ProviderDefinition(
        id="cohere", name="Cohere", description="Command R+ and Aya models for enterprise",
        kind="openai-compatible", base_url="https://api.cohere.ai/v1",
        default_model="command-r-plus-08-2024", aliases=[], icon="🔗",
    ),
    ProviderDefinition(
        id="xai", name="xAI", description="Grok models from xAI",
        kind="openai-compatible", base_url="https://api.x.ai/v1",
        default_model="grok-2-latest", aliases=["grok", "x-ai"], popular=True, icon="✖️",
    ),
    ProviderDefinition(
        id="mistral", name="Mistral", description="Mistral Large, Codestral, and Pixtral models",
        kind="openai-compatible", base_url="https://api.mistral.ai/v1",
        default_model="mistral-large-latest", aliases=["mistral-ai"], popular=True, icon="🌬️",
    ),
    ProviderDefinition(
        id="perplexity", name="Perplexity", description="Sonar models with built-in web search",
        kind="openai-compatible", base_url="https://api.perplexity.ai",
        default_model="llama-3.1-sonar-large-128k-online", aliases=[], icon="🔮",
    ),
    ProviderDefinition(
        id="ollama", name="Ollama", description="Run models locally — Llama, Qwen, Phi, and more",
        kind="ollama", base_url="http://localhost:11434",
        default_model="llama3.2", aliases=[], auth_method="none", popular=True, icon="🐪",
    ),
    ProviderDefinition(
        id="lm-studio", name="LM Studio", description="Local model server with OpenAI-compatible API",
        kind="openai-compatible", base_url="http://localhost:1234/v1",
        default_model="local", aliases=["lmstudio"], auth_method="none", icon="🏠",
    ),
    ProviderDefinition(
        id="vllm", name="vLLM", description="High-throughput local inference server",
        kind="openai-compatible", base_url="http://localhost:8000/v1",
        default_model="auto", aliases=[], auth_method="none", icon="🚀",
    ),
    ProviderDefinition(
        id="azure-openai", name="Azure OpenAI", description="OpenAI models via Azure cloud",
        kind="openai", base_url="", default_model="gpt-4o-mini",
        aliases=["azure"], icon="☁️",
    ),
    ProviderDefinition(
        id="huggingface", name="HuggingFace", description="Inference API for 100k+ models",
        kind="openai-compatible", base_url="https://api-inference.huggingface.co/v1",
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct", aliases=["hf"], icon="🤗",
    ),
    ProviderDefinition(
        id="sambanova", name="SambaNova", description="Fast RDU-based inference for open models",
        kind="openai-compatible", base_url="https://api.sambanova.ai/v1",
        default_model="Meta-Llama-3.1-70B-Instruct", aliases=[], icon="🔴",
    ),
    ProviderDefinition(
        id="novita", name="Novita", description="Affordable API for 100+ open models",
        kind="openai-compatible", base_url="https://api.novita.ai/v3/openai",
        default_model="meta-llama/llama-3.1-70b-instruct", aliases=[], icon="💎",
    ),
    ProviderDefinition(
        id="featherless", name="Featherless", description="Serverless inference for open models",
        kind="openai-compatible", base_url="https://api.featherless.ai/v1/openai",
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct", aliases=[], icon="🪶",
    ),
    ProviderDefinition(
        id="chutes", name="Chutes", description="GPU-accelerated inference for AI workloads",
        kind="openai-compatible", base_url="https://api.chutes.ai/v1",
        default_model="deepseek-ai/DeepSeek-V3", aliases=[], icon="🎯",
    ),
    ProviderDefinition(
        id="aimlapi", name="AI/ML API", description="Unified API for 200+ AI models",
        kind="openai-compatible", base_url="https://api.aimlapi.com/v1",
        default_model="gpt-4o", aliases=["ai-ml-api"], icon="📊",
    ),
    ProviderDefinition(
        id="hyperbolic", name="Hyperbolic", description="GPU marketplace for AI inference",
        kind="openai-compatible", base_url="https://api.hyperbolic.xyz/v1",
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct", aliases=[], icon="📈",
    ),
    ProviderDefinition(
        id="deepseek", name="DeepSeek", description="DeepSeek-V3 and DeepSeek-R1 reasoning models",
        kind="openai-compatible", base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat", aliases=[], popular=True, icon="🐳",
    ),
    ProviderDefinition(
        id="moonshot", name="Moonshot", description="Kimi K1.5 and Moonshot models",
        kind="openai-compatible", base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-8k", aliases=["kimi"], icon="🌙",
    ),
    ProviderDefinition(
        id="voyage-ai", name="Voyage AI", description="Embeddings and reranking models",
        kind="openai-compatible", base_url="https://api.voyageai.com/v1",
        default_model="voyage-large-2", aliases=[], icon="🧭",
    ),
    ProviderDefinition(
        id="nvidia-nim", name="NVIDIA NIM", description="NVIDIA inference microservices for open models",
        kind="openai-compatible", base_url="https://integrate.api.nvidia.com/v1",
        default_model="meta/llama-3.1-70b-instruct", aliases=["nvidia"], icon="💚",
    ),
    ProviderDefinition(
        id="ai21", name="AI21 Labs", description="Jamba models with long context windows",
        kind="openai-compatible", base_url="https://api.ai21.com/v1",
        default_model="jamba-1.5-large", aliases=[], icon="🃏",
    ),
    ProviderDefinition(
        id="databricks", name="Databricks", description="Serving endpoints for fine-tuned models",
        kind="openai-compatible", base_url="", default_model="databricks-dbrx-instruct",
        aliases=[], icon="🧱",
    ),
    ProviderDefinition(
        id="replicate", name="Replicate", description="Run open-source models in the cloud",
        kind="openai-compatible", base_url="https://api.replicate.com/v1",
        default_model="meta/llama-2-70b-chat", aliases=[], icon="🔁",
    ),
    ProviderDefinition(
        id="siliconflow", name="SiliconFlow", description="Cost-effective inference for 200+ models",
        kind="openai-compatible", base_url="https://api.siliconflow.cn/v1",
        default_model="deepseek-ai/DeepSeek-V3", aliases=[], icon="🌊",
    ),
    ProviderDefinition(
        id="nebius", name="Nebius", description="Cloud GPU platform for AI inference",
        kind="openai-compatible", base_url="https://api.studio.nebius.ai/v1",
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct", aliases=[], icon="☁️",
    ),
    ProviderDefinition(
        id="lepton", name="Lepton AI", description="Serverless inference with auto-scaling",
        kind="openai-compatible", base_url="https://api.lepton.ai/v1",
        default_model="meta-llama/Llama-3.2-70B-Instruct", aliases=[], icon="⚛️",
    ),
    ProviderDefinition(
        id="lambda", name="Lambda Labs", description="GPU cloud for AI training and inference",
        kind="openai-compatible", base_url="https://api.lambdalabs.com/v1",
        default_model="", aliases=["lambda-labs"], icon="λ",
    ),
    ProviderDefinition(
        id="kluster", name="Kluster AI", description="Distributed GPU network for inference",
        kind="openai-compatible", base_url="https://api.kluster.ai/v1",
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct", aliases=[], icon="🔗",
    ),
    ProviderDefinition(
        id="upstage", name="Upstage", description="Solar Mini and Pro models for Korean and English",
        kind="openai-compatible", base_url="https://api.upstage.ai/v1",
        default_model="solar-pro", aliases=[], icon="☀️",
    ),
    ProviderDefinition(
        id="yi", name="Yi (01.AI)", description="Yi-Large and Yi-34B models",
        kind="openai-compatible", base_url="https://api.01.ai/v1",
        default_model="yi-large", aliases=["01-ai", "yi-ai"], icon="🎯",
    ),
    ProviderDefinition(
        id="volcengine", name="Volcengine", description="ByteDance's Ark platform for Doubao models",
        kind="openai-compatible", base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model="doubao-pro-32k", aliases=["ark", "bytedance"], icon="🌋",
    ),
    ProviderDefinition(
        id="hunyuan", name="Tencent Hunyuan", description="Tencent's Hunyuan large language models",
        kind="openai-compatible", base_url="https://api.hunyuan.cloud.tencent.com/v1",
        default_model="hunyuan-turbos-latest", aliases=["tencent"], icon="🐧",
    ),
    ProviderDefinition(
        id="cloudflare-ai", name="Cloudflare Workers AI", description="Serverless AI inference at the edge",
        kind="openai-compatible", base_url="", default_model="@cf/meta/llama-3.1-70b-instruct",
        aliases=["cf-ai"], icon="orange",
    ),
    ProviderDefinition(
        id="monsterapi", name="Monster API", description="Affordable GPU inference for open models",
        kind="openai-compatible", base_url="https://api.monsterapi.ai/v1",
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct", aliases=[], icon="👾",
    ),
    ProviderDefinition(
        id="baseten", name="Baseten", description="Model deployment and serving platform",
        kind="openai-compatible", base_url="https://inference.baseten.co/v1",
        default_model="", aliases=[], icon="🏗️",
    ),
    ProviderDefinition(
        id="anyscale", name="Anyscale", description="Ray-based scalable model serving",
        kind="openai-compatible", base_url="https://api.endpoints.anyscale.com/v1",
        default_model="meta-llama/Llama-3.1-70B-Instruct", aliases=[], icon="📐",
    ),
    ProviderDefinition(
        id="jan", name="Jan", description="Local AI model server with web UI",
        kind="openai-compatible", base_url="http://localhost:1337/v1",
        default_model="local", aliases=[], auth_method="none", icon="🖥️",
    ),
    ProviderDefinition(
        id="llamacpp", name="Llama.cpp Server", description="Local C++ inference server",
        kind="openai-compatible", base_url="http://localhost:8080",
        default_model="local", aliases=["llama-cpp"], auth_method="none", icon="🦙",
    ),
    ProviderDefinition(
        id="watsonx", name="IBM Watsonx", description="Enterprise AI platform with Granite models",
        kind="openai-compatible", base_url="", default_model="ibm/granite-3-8b-instruct",
        aliases=["ibm"], icon="🔷",
    ),
    ProviderDefinition(
        id="aleph-alpha", name="Aleph Alpha", description="European sovereign AI — Luminous models",
        kind="openai-compatible", base_url="https://api.aleph-alpha.eu/v1",
        default_model="luminous-supreme-control", aliases=[], icon="α",
    ),
    ProviderDefinition(
        id="predibase", name="Predibase", description="Fine-tune and serve open-source LLMs",
        kind="openai-compatible", base_url="https://serving.app.predibase.com/v1",
        default_model="meta-llama/Meta-Llama-3.1-8B-Instruct", aliases=[], icon="🎯",
    ),
    ProviderDefinition(
        id="gravity", name="Gravity API", description="Multi-model API gateway",
        kind="openai-compatible", base_url="https://api.gravityapi.com/v1",
        default_model="auto", aliases=[], icon="🌌",
    ),
    ProviderDefinition(
        id="infermatic", name="Infermatic", description="Fast inference for popular open models",
        kind="openai-compatible", base_url="https://api.infermatic.ai/v1",
        default_model="meta-llama/Meta-Llama-3.1-70B-Instruct", aliases=[], icon="💡",
    ),
]

_BY_ID: dict[str, ProviderDefinition] = {p.id: p for p in _BUILTIN}


def all_providers() -> list[ProviderDefinition]:
    return list(_BUILTIN)


def popular_providers() -> list[ProviderDefinition]:
    return [p for p in _BUILTIN if p.popular]


def by_id(provider_id: str) -> ProviderDefinition | None:
    return _BY_ID.get(provider_id)


def search(query: str) -> list[ProviderDefinition]:
    q = query.strip().lower()
    if not q:
        return list(_BUILTIN)
    results: list[tuple[int, ProviderDefinition]] = []
    for p in _BUILTIN:
        score = 0
        name_lower = p.name.lower()
        if name_lower == q:
            score = 100
        elif name_lower.startswith(q):
            score = 80
        elif q in name_lower:
            score = 60
        elif any(q in a.lower() for a in p.aliases):
            score = 70
        elif q in p.description.lower():
            score = 30
        elif q in p.id.lower():
            score = 50
        if score > 0:
            results.append((score, p))
    results.sort(key=lambda x: (-x[0], x[1].name))
    return [p for _, p in results]
