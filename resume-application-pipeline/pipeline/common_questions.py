"""Questions US job applications commonly ask. The Dashboard and the Appliers share this wording.

Each item has a stable key, the question as the Dashboard shows it, other wordings that forms use,
and the choices for a dropdown when the answer is one of a fixed set. Items without options take free text.
Demographic items are the voluntary self-identification questions; the rest are application questions.
"""
from __future__ import annotations

from dataclasses import dataclass

DECLINE = 'Decline to self-identify'
YES_NO = ('Yes', 'No')


@dataclass(frozen=True)
class CommonQuestion:
    key: str
    group: str
    question: str
    aliases: tuple[str, ...]
    options: tuple[str, ...] = ()


CATALOG = (
    CommonQuestion('age_18', 'demographic', 'Are you at least 18 years old?', (
        'Are you over 18?', 'Are you 18 years of age or older?', 'Are you 18 or older?', 'Are you at least 18 years of age?',
        'Are you over the age of 18?', 'Are you of legal working age?', 'Age 18 or older',
    ), YES_NO),
    CommonQuestion('gender', 'demographic', 'What is your gender?', (
        'Gender', 'Gender identity', 'What is your gender identity?', 'Please select your gender', 'Sex',
        'How do you identify your gender?',
    ), ('Male', 'Female', 'Non-binary', DECLINE)),
    CommonQuestion('hispanic_latino', 'demographic', 'Are you Hispanic or Latino?', (
        'Hispanic or Latino', 'Hispanic/Latino', 'Are you Hispanic/Latino?', 'Do you identify as Hispanic or Latino?',
        'Are you of Hispanic or Latino origin?', 'Hispanic or Latino ethnicity',
    ), ('Yes', 'No', DECLINE)),
    CommonQuestion('race', 'demographic', 'What is your race or ethnicity?', (
        'Race', 'Ethnicity', 'Race/Ethnicity', 'Race / Ethnicity', 'Race or ethnicity', 'What is your race?',
        'What is your ethnicity?', 'Please identify your race', 'Please identify your race or ethnicity',
        'Please select your race', 'Please select your ethnicity', 'Which race or ethnicity best describes you?',
    ), ('American Indian or Alaska Native', 'Asian', 'Black or African American', 'Hispanic or Latino',
        'Native Hawaiian or Other Pacific Islander', 'White', 'Two or More Races', DECLINE)),
    CommonQuestion('veteran', 'demographic', 'What is your veteran status?', (
        'Veteran status', 'Protected veteran status', 'Are you a protected veteran?', 'Are you a veteran?',
        'Please select your veteran status', 'Do you identify as a protected veteran?',
        'Have you served in the U.S. military?',
    ), ('I am not a protected veteran', 'I identify as one or more of the classifications of protected veteran',
        "I don't wish to answer")),
    CommonQuestion('disability', 'demographic', 'Do you have a disability?', (
        'Disability status', 'Disability', 'Voluntary self-identification of disability',
        'Do you have a disability or have you had one in the past?', 'Please indicate your disability status',
        'Do you identify as a person with a disability?',
    ), ('Yes, I have a disability (or previously had a disability)',
        'No, I do not have a disability and have not had one in the past', 'I do not want to answer')),
    CommonQuestion('sexual_orientation', 'demographic', 'What is your sexual orientation?', (
        'Sexual orientation', 'How do you describe your sexual orientation?', 'Please select your sexual orientation',
        'Do you identify as LGBTQ+?',
    ), ('Heterosexual or straight', 'Gay', 'Lesbian', 'Bisexual', 'Queer', 'Asexual', DECLINE)),
    CommonQuestion('transgender', 'demographic', 'Do you identify as transgender?', (
        'Transgender', 'Are you transgender?', 'Do you consider yourself transgender?',
        'Do you identify as transgender or gender non-conforming?',
    ), ('Yes', 'No', DECLINE)),
    CommonQuestion('pronouns', 'demographic', 'What are your pronouns?', (
        'Pronouns', 'Preferred pronouns', 'Your pronouns', 'What pronouns do you use?',
    )),
    CommonQuestion('work_authorization', 'application', 'Are you legally authorized to work in the United States?', (
        'Are you authorized to work for any employer in the United States of America?',
        'Are you currently eligible to work in the United States of America?', 'Are you authorized to work in the U.S.?',
        'Are you authorized to work in the United States?', 'Are you legally authorized to work in the U.S.?',
        'Are you authorized to work for any employer in the United States?', 'Are you currently eligible to work in the United States?',
        'Are you legally eligible to work in the United States?', 'Are you currently authorized to work in the United States?',
        'Work authorization', 'US work authorization',
    ), YES_NO),
    CommonQuestion('sponsorship', 'application', 'Will you now or in the future require visa sponsorship to work in the United States?', (
        'Do you now, or will you at any point in the future, require sponsorship?',
        'In order to begin or continue employment in the United States, will you now or in the future require sponsorship or assistance?',
        'Will you now or at any time in the future require sponsorship for employment visa status (e.g. H1B, OPT)?',
        'Will you now or in the future require sponsorship?', 'Will you now or in the future require visa sponsorship?',
        'Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)?',
        'Do you now or will you in the future require sponsorship for employment visa status?',
        'Will you now, or in the future, require sponsorship for employment visa status?',
        'Do you require visa sponsorship?', 'Will you require visa sponsorship?', 'Visa sponsorship', 'Sponsorship required',
    ), YES_NO),
    CommonQuestion('visa_status', 'application', 'What is your current visa or immigration status?', (
        'What is your current visa status?', 'Visa status', 'Current visa status', 'Immigration status',
        'What is your immigration status?', 'What is your current immigration status?', 'What type of visa do you currently hold?',
    )),
    CommonQuestion('relocation', 'application', 'Are you willing to relocate?', (
        'Willing to relocate', 'Relocation', 'Would you be willing to relocate?', 'Are you open to relocation?',
        'Are you open to relocating?', 'Are you willing to relocate for this role?', 'Are you willing to relocate for this position?',
        'Please confirm that you are willing to relocate for this role', 'Would you consider relocating for this role?',
    ), YES_NO),
    CommonQuestion('onsite', 'application', 'Are you willing to work onsite in the office?', (
        'Are you willing to work onsite?', 'Are you able to work onsite?', 'Are you open to working onsite?',
        'Are you willing to work in the office?', 'Are you comfortable working in the office?',
        'Are you able to work in the office 5 days a week?', 'Are you able to work in the office five days a week?',
        'Are you able to work onsite 5 days a week?', 'If US based, are you OK with working in office 5 days/ week?',
        'Are you willing to work in person?', 'Are you able to work in person?', 'Are you comfortable with the in-office requirement?',
        'Are you open to a hybrid work arrangement?', 'Are you able to work in a hybrid environment?', 'In-office requirement',
    ), YES_NO),
    CommonQuestion('preferred_locations', 'application', 'Which locations would you like to work from?', (
        'From where do you intend to work?', 'Where would you like to work?', 'Preferred location', 'Preferred locations',
        'Preferred work location', 'Preferred office location', 'What are your preferred office locations?',
        'Which office location do you prefer?', 'Which locations are you open to working in?',
        'Select all locations you would be open to being placed',
        'Please indicate all of the locations that you would be interested in relocating to for this position',
        'Please indicate all locations that you would be interested in relocating to for this position',
        'Which locations would you be willing to relocate to?',
    )),
    CommonQuestion('start_date', 'application', 'When can you start?', (
        'When can you start a new role?', 'What is your anticipated start date? (Month/Year)', 'What is your anticipated start date?',
        'Anticipated start date', 'Start date', 'Earliest start date', 'What is your earliest start date?',
        'When are you available to start?', 'When would you be available to start?', 'Desired start date',
        'What is your desired start date?', 'When could you start?',
    )),
    CommonQuestion('sms_consent', 'application', 'Do you consent to receive text messages about your application?', (
        'SMS consent', 'Do you consent to receive SMS messages about your application?',
        'Do you agree to receive text messages about your application?', 'Do you consent to receiving text messages?',
        'Would you like to receive text messages about your application?',
        'Would you like to receive text message updates about your application?',
        'Would you like to receive communications via SMS and/or WhatsApp to the number provided '
        '[above or during the application process] about your application process?',
    ), YES_NO),
    CommonQuestion('background_check', 'application', 'Are you willing to undergo a background check?', (
        'Background check', 'Are you willing to submit to a background check?', 'Will you consent to a background check?',
        'Are you willing to complete a background check?', 'Are you willing to undergo a criminal background check?',
        'Do you consent to a background check?',
    ), YES_NO),
    CommonQuestion('drug_test', 'application', 'Are you willing to take a drug test?', (
        'Drug test', 'Drug screening', 'Are you willing to undergo a drug test?', 'Are you willing to submit to a drug screening?',
        'Are you willing to take a pre-employment drug test?', 'Will you consent to a drug test?',
    ), YES_NO),
    CommonQuestion('drivers_license', 'application', "Do you have a valid driver's license?", (
        "Driver's license", 'Drivers license', "Do you hold a valid driver's license?", "Do you have a current driver's license?",
        "Do you possess a valid driver's license?", 'Do you have a valid drivers license?',
    ), YES_NO),
    CommonQuestion('security_clearance', 'application', 'What U.S. security clearance do you hold?', (
        'Security clearance', 'Do you have a security clearance?', 'Do you have an active security clearance?',
        'Do you hold an active U.S. security clearance?', 'Do you currently hold a security clearance?',
        'What is your current security clearance level?', 'Active security clearance',
    ), ('None', 'Public Trust', 'Confidential', 'Secret', 'Top Secret', 'Top Secret/SCI')),
    CommonQuestion('government_employee', 'application', 'Are you a current or former U.S. government employee?', (
        'Are you a current or former government employee?', 'Have you ever been employed by the U.S. government?',
        'Are you currently or have you ever been a federal government employee?', 'Current or former government employee',
        'Have you worked for the federal government?',
    ), YES_NO),
    CommonQuestion('travel', 'application', 'Are you willing to travel for work?', (
        'Willingness to travel', 'Are you willing to travel?', 'Are you able to travel for work?',
        'Are you willing to travel as required for this role?', 'Can you travel for work?',
    ), YES_NO),
    CommonQuestion('referral_source', 'application', 'How did you hear about us?', (
        'How did you hear about this job?', 'How did you hear about this position?', 'How did you hear about this opportunity?',
        'How did you find out about this role?', 'Where did you hear about us?', 'How did you learn about us?',
    )),
    CommonQuestion('desired_salary', 'application', 'What is your desired salary?', (
        'Desired salary', 'Salary expectations', 'What are your salary expectations?', 'Expected salary',
        'What is your expected salary?', 'Desired compensation', 'What are your compensation expectations?',
        'Desired pay',
    )),
    CommonQuestion('years_experience', 'application', 'How many years of professional experience do you have?', (
        'Years of experience', 'Years of professional experience', 'How many years of experience do you have?',
        'Total years of experience', 'How many years of relevant experience do you have?',
    )),
    CommonQuestion('graduation_date', 'application', 'What is your graduation date?', (
        'Graduation date', 'Expected graduation date', 'When did you graduate?', 'When do you expect to graduate?',
        'Graduation year', 'What is your expected graduation date?',
    )),
    CommonQuestion('gpa', 'application', 'What is your GPA?', (
        'GPA', 'Cumulative GPA', 'Overall GPA', 'What is your cumulative GPA?', 'Grade point average',
    )),
    CommonQuestion('non_compete', 'application',
                   'Are you subject to a non-compete or other restrictive agreement with a current or former employer?', (
        'Are you bound by a non-compete agreement?', 'Do you have a non-compete agreement?',
        'Are you subject to any non-compete, non-solicitation or confidentiality agreement?',
        'Are you party to any agreement that would restrict your ability to work for us?',
        'Non-compete agreement', 'Are you subject to a non-compete agreement?',
    ), YES_NO),
)
