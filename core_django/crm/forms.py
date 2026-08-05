from django import forms

from .models import Campaign, ContactNote
from .services.campaigns import ALLOWED_VARIABLES, extract_placeholders


class BasecoatMixin:
    """Apply Basecoat's classes to every widget so forms look native."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "select")
            elif isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "input-checkbox")
            elif isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("class", "textarea")
            else:
                widget.attrs.setdefault("class", "input")


class CampaignForm(BasecoatMixin, forms.ModelForm):
    #: Entered as a comma-separated list; stored as the JSON list the model wants.
    var_list_raw = forms.CharField(
        required=False,
        label="Variables",
        help_text="Comma-separated, e.g. first_name, company, designation",
    )

    class Meta:
        model = Campaign
        fields = ["title", "mail_sub", "mail_body"]
        widgets = {"mail_body": forms.Textarea(attrs={"rows": 14})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["var_list_raw"].initial = ", ".join(self.instance.var_list or [])

    def clean_var_list_raw(self):
        raw = self.cleaned_data["var_list_raw"]
        return [v.strip() for v in raw.split(",") if v.strip()]

    def clean(self):
        """Catch template mistakes here, where they are cheap to fix.

        `{{ compnay }}` discovered at send time means a broken mail to a real
        prospect; discovered here it is a red line under a text box.
        """
        cleaned = super().clean()
        declared = set(cleaned.get("var_list_raw") or [])
        probe = Campaign(
            mail_sub=cleaned.get("mail_sub") or "",
            mail_body=cleaned.get("mail_body") or "",
        )
        used = extract_placeholders(probe)

        unknown = used - ALLOWED_VARIABLES
        if unknown:
            self.add_error(
                "mail_body",
                "Not real Contact fields: " + ", ".join(sorted(unknown))
                + ". Available: " + ", ".join(sorted(ALLOWED_VARIABLES)),
            )

        undeclared = (used - declared) - unknown
        if undeclared:
            self.add_error(
                "var_list_raw",
                "Used in the template but not declared: " + ", ".join(sorted(undeclared)),
            )

        unused = declared - used
        if unused:
            self.add_error(
                "var_list_raw",
                "Declared but never used: " + ", ".join(sorted(unused)),
            )
        return cleaned

    def save(self, commit=True):
        campaign = super().save(commit=False)
        campaign.var_list = self.cleaned_data["var_list_raw"]
        if commit:
            campaign.save()
        return campaign


class NoteForm(BasecoatMixin, forms.ModelForm):
    class Meta:
        model = ContactNote
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Add a note…"})}


class CsvUploadForm(forms.Form):
    file = forms.FileField(
        label="CSV file",
        help_text="Required columns: first_name, email, company. "
                  "Optional: last_name, phone_no, linkedin, designation.",
    )


class TokenForm(BasecoatMixin, forms.Form):
    member = forms.ModelChoiceField(queryset=None, label="Team member")
    label = forms.CharField(required=False, label="Label",
                            widget=forms.TextInput(attrs={"placeholder": "e.g. Aarav's MacBook"}))

    def __init__(self, *args, **kwargs):
        members = kwargs.pop("members")
        super().__init__(*args, **kwargs)
        self.fields["member"].queryset = members
