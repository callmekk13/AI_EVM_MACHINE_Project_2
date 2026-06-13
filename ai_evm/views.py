from django.shortcuts import HttpResponse, render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.conf import settings 
from django.core.mail import EmailMessage 
from django.db.models import Count
from django.views.decorators.http import require_POST
from threading import Thread
from .models import Voter, Party, Vote

try:
    from mailjet_rest import Client
except ImportError:
    Client = None


'''
1. Persons detections
2. Mask detection
3. Face Recognition
4. Vote 
'''

PHASES = [
    {
        'key': 'phase_1',
        'title': 'Person Detection',
        'description': 'Confirm exactly one voter is present in the booth.',
        'stream': 'detect_person',
    },
    {
        'key': 'phase_2',
        'title': 'Mask Detection',
        'description': 'Ask the voter to remove their mask for identity checks.',
        'stream': 'detect_mask',
    },
    {
        'key': 'phase_3',
        'title': 'Face Recognition',
        'description': 'Match the voter against registered constituency records.',
        'stream': 'recognize_face',
    },
    {
        'key': 'phase_4',
        'title': 'Secure Vote',
        'description': 'Record one confidential vote for an eligible voter.',
        'stream': None,
    },
]


def init_session(request):
    request.session['phase_1'] = False
    request.session['phase_2'] = False
    request.session['phase_3'] = False
    request.session['phase_4'] = False


def ensure_session(request):
    if 'phase_1' not in request.session:
        init_session(request)


def registered_names():
    return set(Voter.objects.values_list('first_name', flat=True))


def get_current_phase(request):
    ensure_session(request)
    for index, phase in enumerate(PHASES, start=1):
        phase['complete'] = bool(request.session.get(phase['key']))
        phase['active'] = not phase['complete']
        phase['number'] = index
        if phase['active']:
            return phase
    return PHASES[-1]


def build_context(request, **extra):
    current_phase = get_current_phase(request)
    context = {
        'stream': current_phase['stream'],
        'phase': current_phase,
        'phases': PHASES,
        'total_voters': Voter.objects.count(),
        'total_parties': Party.objects.count(),
        'total_votes': Vote.objects.count(),
    }
    context.update(extra)
    return context


def render_index(request, **extra):
    return render(request, 'index.html', build_context(request, **extra))


def start(request):
    ensure_session(request)

    if request.method == 'POST':
        if not request.session['phase_1']:
            # get # of persons
            from detect_person.camera import PERSON_COUNT 
            request.session['persons'] = PERSON_COUNT
            print('PERSONS:', PERSON_COUNT)

            if PERSON_COUNT == 1:
                messages.success(request, 'Persons Detection Phase completed')
                request.session['phase_1'] = True
                return render_index(request)
            else:
                messages.error(request, 'More than one person not allowed in the Polling Booth!')
                return render_index(request)

        elif not request.session['phase_2']:
            # get mask status
            from detect_mask.camera import HAS_MASK
            request.session['has_mask'] = HAS_MASK
            print('HAS_MASK:', HAS_MASK)

            if not HAS_MASK:
                # mark as complete & render phase-3
                messages.success(request, 'Mask Detection Phase completed')
                request.session['phase_2'] = True
                return render_index(request)
            else:
                # revert back to same phase
                messages.warning(request, 'Please remove your mask!')
                return render_index(request)
        
        elif not request.session['phase_3']:
            # get face name
            from recognize_face.camera import FACE_NAME
            request.session['face_name'] = FACE_NAME
            print('FACE_NAME:', FACE_NAME)

            if FACE_NAME in registered_names():
                # mark as complete & move to phase_4 (voting)
                messages.success(request, 'Face Recognition Phase completed')
                request.session['phase_3'] = True
                # if alread voted clear session & send back to main page
                request.method = 'GET'
                return start(request)
            else:
                messages.error(request, 'Your nomination is not in this constituency!')
                return render_index(request)

        elif not request.session['phase_4']:
            if 'voted_to' not in request.POST:
                messages.warning(request, 'Please select a candidate before submitting your vote.')
                return render_index(request, pts=Party.objects.all())

            voted_to = Party.objects.get(name=request.POST['voted_to'])
            voter = Voter.objects.get(first_name=request.session['face_name'])
            Vote(voter=voter, voted_to=voted_to).save()
            Thread(target= success, args=(voter, voted_to.full_name)).start()
            # return JsonResponse(dict(request.POST))
            messages.success(request, 'Thank for you vote! Your vote has been received!')
            request.session.flush()
            request.method = 'GET'
            return start(request)
        
        else:
            return HttpResponse('No suitable POST condition satisfied!')

    else:
        if not request.session['phase_1']:
            return render_index(request)
        if not request.session['phase_2']:
            return render_index(request)
        if not request.session['phase_3']:
            return render_index(request)
        if not request.session['phase_4']:
            if len(Vote.objects.filter(voter__first_name=request.session['face_name'])) > 0:
                messages.error(request, 'Sorry, You have already voted!')
                request.session.flush()
                request.method = 'GET'
                return start(request)
            else:
                return render_index(request, pts=Party.objects.all())




def dbg(request):
    return JsonResponse(dict(request.session))


def api_status(request):
    current_phase = get_current_phase(request)
    return JsonResponse({
        'current_phase': current_phase['key'],
        'current_title': current_phase['title'],
        'phases': [
            {
                'key': phase['key'],
                'title': phase['title'],
                'complete': bool(request.session.get(phase['key'])),
                'active': phase['key'] == current_phase['key'],
            }
            for phase in PHASES
        ],
        'session': {
            'persons': request.session.get('persons'),
            'has_mask': request.session.get('has_mask'),
            'face_name': request.session.get('face_name'),
        },
        'stats': {
            'voters': Voter.objects.count(),
            'parties': Party.objects.count(),
            'votes': Vote.objects.count(),
        }
    })


def api_results(request):
    results = (
        Party.objects
        .annotate(vote_count=Count('to'))
        .values('name', 'full_name', 'vote_count')
        .order_by('-vote_count', 'name')
    )
    return JsonResponse({'results': list(results)})


@require_POST
def reset_session(request):
    request.session.flush()
    messages.info(request, 'Session reset. Start the verification flow again.')
    return redirect('start')


def success(voter, voted_to):
    if Client is None or not settings.MAILJET_API_KEY or not settings.MAILJET_API_SECRET:
        print(f"Skipping email send for {voter.email}: Mailjet not configured")
        return

    mailjet = Client(auth=(settings.MAILJET_API_KEY, settings.MAILJET_API_SECRET), version='v3.1')
    data = {
    'Messages': [
        {
        "From": {
            "Email": "nktchhn1997@gmail.com",
            "Name": "Ankit"
        },
        "To": [
            {
            "Email": voter.email, # In future make sure to query by pk
            "Name": voter.first_name
            }
        ],
        "Subject": "Greetings AI-EVM.",
        "TextPart": "Your vote has been counted",
        "HTMLPart": f"<h3>Dear {voter.first_name}, This mail is to remind you that your vote has been taken into consideration! <br> You have voted to {voted_to} </h3><br />Thank You!",
        "CustomID": "AppGettingStartedTest"
        }
    ]
    }
    result = mailjet.send.create(data=data)
    print(result.status_code)
