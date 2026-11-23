"""Owner-facing forms. Services remain the authority for permissions and arithmetic."""
from decimal import Decimal, InvalidOperation
from django import forms
from django.forms import formset_factory
from . import money
from .models import BusinessSettings, Customer, Document, Payment, Product, TaxJurisdiction


class DollarsField(forms.CharField):
    def __init__(self, *args, minimum=0, **kwargs):
        self.minimum = minimum
        kwargs.setdefault('widget', forms.TextInput(attrs={'inputmode': 'decimal'}))
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        if value in self.empty_values:
            return None
        try:
            cents = money.parse_usd(str(value))
            if cents < self.minimum or cents > 2_000_000_000:
                raise ValueError('amount out of range')
            return cents
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc


class PercentField(forms.CharField):
    def to_python(self, value):
        if value in self.empty_values:
            return None
        try:
            bp = Decimal(str(value).strip().rstrip('%')) * 100
            if not bp.is_finite() or bp != bp.to_integral_value() or not 0 <= bp <= 10000:
                raise ValueError
            return int(bp)
        except (InvalidOperation, ValueError, OverflowError):
            raise forms.ValidationError('Use a percentage from 0 to 100 with at most 2 decimal places.')


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ['name', 'email', 'phone', 'address', 'notes', 'tax_exempt', 'exemption_note']
        widgets = {'address': forms.Textarea(attrs={'rows': 3}), 'notes': forms.Textarea(attrs={'rows': 3})}


class TaxLocationForm(forms.Form):
    state = forms.RegexField(r'^[A-Za-z]{2}$', max_length=2, initial='IA')
    jurisdiction = forms.ModelChoiceField(TaxJurisdiction.objects.order_by('name'), required=False)
    checked_on = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    source = forms.CharField(max_length=200)

    def clean(self):
        data = super().clean()
        state = data.get('state', '').upper()
        data['state'] = state
        loc = data.get('jurisdiction')
        if state == 'IA' and not loc:
            self.add_error('jurisdiction', 'Choose a city or rural county.')
        if loc and loc.state != state:
            self.add_error('jurisdiction', 'The jurisdiction must match the state.')
        return data


class DraftForm(forms.ModelForm):
    late_fee_bp = PercentField(label='Late fee per 30 days (%)', required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.late_fee_bp is not None:
            self.initial['late_fee_bp'] = money.format_rate(self.instance.late_fee_bp).rstrip('%')

    class Meta:
        model = Document
        fields = ['customer', 'notes', 'donated', 'late_fee_enabled', 'late_fee_bp', 'tax_override_state', 'tax_override_jurisdiction', 'email_subject', 'email_body']
        widgets = {'notes': forms.Textarea(attrs={'rows': 3}), 'email_body': forms.Textarea(attrs={'rows': 4})}

    def clean(self):
        data = super().clean()
        state = data.get('tax_override_state', '').upper()
        data['tax_override_state'] = state
        loc = data.get('tax_override_jurisdiction')
        if loc and loc.state != state:
            self.add_error('tax_override_jurisdiction', 'Choose the matching override state.')
        if state == 'IA' and not loc:
            self.add_error('tax_override_jurisdiction', 'Choose an Iowa jurisdiction.')
        return data


class LineForm(forms.Form):
    product = forms.ModelChoiceField(Product.objects.filter(active=True), required=False, empty_label='Custom item')
    description = forms.CharField(max_length=500, widget=forms.TextInput(attrs={'placeholder': 'What was done or sold'}))
    quantity = forms.CharField(label='Qty', initial='1', widget=forms.TextInput(attrs={'inputmode': 'decimal'}))
    rate = DollarsField(label='Rate', widget=forms.TextInput(attrs={'inputmode': 'decimal', 'placeholder': '0.00'}))
    discount = DollarsField(label='Discount', required=False, widget=forms.TextInput(attrs={'inputmode': 'decimal', 'placeholder': '0.00'}))
    discount_label = forms.CharField(label='Shown on the invoice as', max_length=200, required=False, widget=forms.TextInput(attrs={'placeholder': 'e.g. Nonprofit discount'}))
    taxable = forms.BooleanField(required=False)

    def clean_quantity(self):
        try:
            q = Decimal(self.cleaned_data['quantity']) * 1000
            if not q.is_finite() or q != q.to_integral_value() or not 0 < q <= 2_000_000_000:
                raise ValueError
            return int(q)
        except (InvalidOperation, ValueError, OverflowError):
            raise forms.ValidationError('Use a positive quantity with at most 3 decimal places.')

    def clean(self):
        data = super().clean()
        if {'rate', 'quantity'} <= data.keys():
            try:
                money.LineInput(data['rate'], data['quantity'], data.get('discount') or 0, data.get('taxable', False))
            except ValueError as exc:
                raise forms.ValidationError(str(exc))
        return data

    def line_data(self):
        d = self.cleaned_data
        return dict(product=d.get('product'), description=d['description'], qty_milli=d['quantity'], unit_cents=d['rate'], discount_cents=d.get('discount') or 0, discount_label=d.get('discount_label', ''), taxable=d['taxable'])


LineFormSet = formset_factory(LineForm, extra=1, min_num=1, validate_min=True, can_delete=True, max_num=200, validate_max=True, absolute_max=200)


class PaymentForm(forms.Form):
    invoice = forms.ModelChoiceField(Document.objects.filter(kind='invoice', status__in=['approved', 'sent', 'partly_paid']))
    method = forms.ChoiceField(choices=[(Payment.Method.CHECK, 'Check'), (Payment.Method.CASH, 'Cash')])
    amount = DollarsField(label='Amount ($)', minimum=1)
    received_on = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    check_number = forms.CharField(max_length=40, required=False)
    note = forms.CharField(max_length=300, required=False)
    email_receipt = forms.BooleanField(initial=True, required=False)


class SendForm(forms.Form):
    to = forms.EmailField(label='To')
    subject = forms.CharField(max_length=200)
    body = forms.CharField(widget=forms.Textarea(attrs={'rows': 10}))

    def clean_subject(self):
        subject = self.cleaned_data['subject']
        if '\n' in subject or '\r' in subject:
            raise forms.ValidationError('Use a single line for the subject.')
        return subject


class SettingsForm(forms.ModelForm):
    min_online_payment_cents = DollarsField(label='Minimum online payment ($)', minimum=50)
    late_fee_default_bp = PercentField(label='Default late fee per 30 days (%)')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial['min_online_payment_cents'] = money.format_usd(self.instance.min_online_payment_cents)
            self.initial['late_fee_default_bp'] = money.format_rate(self.instance.late_fee_default_bp).rstrip('%')

    class Meta:
        model = BusinessSettings
        fields = ['name', 'address_line1', 'city_state_zip', 'phone', 'email', 'min_online_payment_cents', 'late_fee_default_bp', 'send_warning_days']


class PeriodForm(forms.Form):
    start = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))

    def clean(self):
        data = super().clean()
        if data.get('start') and data.get('end') and data['start'] > data['end']:
            raise forms.ValidationError('The end date must be on or after the start date.')
        return data


class PayForm(forms.Form):
    choice=forms.ChoiceField(label='Payment amount',choices=[('full','Pay the full balance'),('other','Pay another amount')],widget=forms.RadioSelect,initial='full')
    amount=DollarsField(label='Other amount ($)',required=False,minimum=1)
    request_id=forms.UUIDField(widget=forms.HiddenInput)

    def clean(self):
        data=super().clean()
        if data.get('choice')=='other' and data.get('amount') is None:
            self.add_error('amount','Enter the amount you want to pay.')
        return data


class RefundResolutionForm(forms.Form):
    resolution=forms.ChoiceField(choices=[('reopen','Reopen the refunded balance'),('credit_note','Approve reviewed credit note and settle refund')])
    credit_note=forms.ModelChoiceField(Document.objects.none(),required=False,label='Reviewed credit-note draft')

    def __init__(self,invoice,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['credit_note'].queryset=Document.objects.filter(kind='credit_note',credit_for=invoice,status='draft',source='app')

    def clean(self):
        data=super().clean()
        if data.get('resolution')=='credit_note' and not data.get('credit_note'):
            self.add_error('credit_note','Choose a draft for the refunded work, with its tax treatment reviewed.')
        return data
