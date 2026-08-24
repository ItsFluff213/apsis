using UnityEngine;

namespace Apsis
{
    /// <summary>
    /// Lets you pick a craft's role (and optional stage number) in-game via
    /// the same stock left/right cycler control used for things like fuel
    /// tank "Change Storage Setup" -- rather than typing free text into the
    /// part's Tag field.
    ///
    /// This module writes the exact same tag string the dashboard/kRPC side
    /// already reads (see backend/parts.py's get_vessel_role_tag): plain
    /// "booster", or "booster.stage1" if a stage number is picked. It's
    /// added only to parts with ModuleCommand (probe cores, command pods --
    /// i.e. whatever kRPC's Parts.controlling would return), via the
    /// ModuleManager patch in Apsis_MM.cfg.
    /// </summary>
    public class ModuleApsisRole : PartModule
    {
        private static readonly string[] Roles =
        {
            "none", "booster", "satellite", "docking", "station", "capsule", "lander", "probe",
        };

        private static readonly string[] Stages =
        {
            "none", "1", "2", "3", "4", "5", "6", "7", "8", "9",
        };

        [KSPField(isPersistant = true, guiActive = true, guiActiveEditor = true, guiName = "Role"),
         UI_ChooseOption(options = new[] { "none", "booster", "satellite", "docking", "station", "capsule", "lander", "probe" })]
        public string role = "none";

        [KSPField(isPersistant = true, guiActive = true, guiActiveEditor = true, guiName = "Stage"),
         UI_ChooseOption(options = new[] { "none", "1", "2", "3", "4", "5", "6", "7", "8", "9" })]
        public string stage = "none";

        public override void OnStart(StartState state)
        {
            base.OnStart(state);
            ApplyTag();

            var roleField = Fields["role"];
            roleField.uiControlEditor.onFieldChanged += (_, __) => ApplyTag();
            roleField.uiControlFlight.onFieldChanged += (_, __) => ApplyTag();

            var stageField = Fields["stage"];
            stageField.uiControlEditor.onFieldChanged += (_, __) => ApplyTag();
            stageField.uiControlFlight.onFieldChanged += (_, __) => ApplyTag();

            // If the part already has a tag (e.g. set via the stock Tag
            // field, or loaded from a save), reflect it back into these
            // controls so they don't silently disagree with reality.
            ReadBackExistingTag();
        }

        private void ReadBackExistingTag()
        {
            var tag = part.tag;
            if (string.IsNullOrEmpty(tag)) return;

            var dot = tag.IndexOf('.');
            var category = dot >= 0 ? tag.Substring(0, dot) : tag;
            var detail = dot >= 0 ? tag.Substring(dot + 1) : "";

            if (System.Array.IndexOf(Roles, category) >= 0) role = category;
            if (detail.StartsWith("stage") && System.Array.IndexOf(Stages, detail.Substring(5)) >= 0)
            {
                stage = detail.Substring(5);
            }
        }

        private void ApplyTag()
        {
            if (role == "none")
            {
                part.tag = "";
            }
            else if (stage != "none")
            {
                part.tag = role + ".stage" + stage;
            }
            else
            {
                part.tag = role;
            }
        }
    }
}
