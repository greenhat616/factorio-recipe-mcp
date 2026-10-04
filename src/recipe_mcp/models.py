"""Pydantic models for query results and tool inputs.

Raw prototypes and the save snapshot stay plain JSON (``JSON``): they are large,
follow the game's data format, and only a few fields of each are read.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict

JSON = dict[str, Any]


def entries(value: Any) -> list[Any]:
    """Lua empty tables export as {}; non-empty arrays as lists."""
    return list(value.values()) if isinstance(value, dict) else list(value or [])


# Arrays read from exported Lua data, which may arrive as {} when empty.
LuaList = Annotated[list[JSON], BeforeValidator(entries)]
LuaStrList = Annotated[list[str], BeforeValidator(entries)]

RESEARCH_GATE = 'research gate only; machines, ingredients and surface conditions are separate'


class Spec(BaseModel):
    """Structured tool input. Unknown keys are errors, so a misspelt option is not silently ignored."""

    model_config = ConfigDict(extra='forbid')


class Availability(BaseModel):
    state: Literal['virtual', 'unknown', 'unlocked', 'script_disabled', 'locked', 'missing']
    usable_at_stage: bool | None
    reason: str | None = None
    force: str | None = None
    enabled_in_save: bool | None = None
    researched_unlocks: list[str] = []
    locked_unlock_alternatives: list[str] = []
    productivity_bonus: float = 0
    scope: str = RESEARCH_GATE


class RecipeInfo(BaseModel):
    name: str
    category: str
    energy_required: float
    ingredients: LuaList
    results: LuaList
    # Defaults are the game's own, so an absent key reads the same as in-game.
    allow_productivity: bool = False
    maximum_productivity: float = 3.0
    allow_quality: bool = True
    hidden: bool = False
    localised_name: Any = None
    surface_conditions: LuaList | None = None
    enabled_at_start: bool
    unlock_technologies: list[str]
    virtual: bool
    availability: Availability


class Construction(BaseModel):
    """Whether a machine or module can be manufactured at the force's stage."""

    machine: str | None = None
    item: str | None = None
    buildable_at_stage: bool | None
    manufacturing_recipes: list[str] = []
    reason: str | None = None
    scope: str = 'manufacturing research gate only; existing inventory not inspected'


class MachineInfo(BaseModel):
    name: str
    type: str
    crafting_speed: float | None = None
    energy_usage: str | None = None
    energy_source: JSON | None = None
    module_slots: int | None = None
    allowed_effects: LuaStrList | None = None
    effect_receiver: JSON | None = None
    fluid_boxes: LuaList | None = None
    fixed_recipe: str | None = None
    construction: Construction


TechnologyState = Literal['researched', 'researching', 'available_to_research', 'locked', 'disabled', 'unknown']


class TechnologyInfo(BaseModel):
    name: str
    localised_name: Any = None
    unit: JSON | None = None
    research_trigger: JSON | None = None
    effects: LuaList = []
    hidden: bool = False
    max_level: int | str | None = None
    upgrade: bool = False
    prerequisites: LuaStrList
    unlocks_recipes: list[str]
    enabled_at_start: bool
    force: str | None
    state: TechnologyState
    runtime: JSON | None
    missing_prerequisites: list[str] | None
    progress: float | None
    note: str = 'available_to_research checks prerequisite/enabled flags, not science supply or trigger completion'


class TechnologyPage(BaseModel):
    total: int
    offset: int
    limit: int
    technologies: list[TechnologyInfo]


class ScienceRequirements(BaseModel):
    technology: str
    science_packs: list[str]
    # Nullius checkpoint tokens: unit ingredients that are not science packs.
    requirement_tokens: list[str]
    prerequisites_transitive: list[str]
    technology_state: TechnologyInfo


class ForceSummary(BaseModel):
    name: str
    researched_count: int
    enabled_recipe_count: int
    current_research: str | None
    research_progress: float | None
    research_queue: LuaStrList


class ProgressContext(BaseModel):
    snapshot_loaded: bool
    compatible: bool
    default_force: str | None
    tick: int | None
    provenance: JSON | None
    forces: list[ForceSummary]


class RecipeGate(BaseModel):
    recipe: str
    availability: Availability


class PlanValidation(BaseModel):
    force: str
    valid_at_stage: bool
    recipes: list[RecipeGate]
    blocked_recipes: list[str]
    equipment: list[Construction]
    blocked_equipment: list[Construction]
    scope: str = 'research gates only; does not prove ingredient supply, compatible machines or surface conditions'


class RelatedRecipes(BaseModel):
    producers: list[RecipeInfo] | None = None
    consumers: list[RecipeInfo] | None = None


class ProductionChain(BaseModel):
    recipes: list[RecipeInfo]
    unexpanded_materials: list[str]
    truncated: bool
